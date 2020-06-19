# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
from tensorboardX import SummaryWriter
from scipy.io.wavfile import write
from collections import OrderedDict
import argparse
from pprint import pprint
from ruamel import yaml
from matplotlib import cm
import numpy as np
import paddle.fluid as fluid
import paddle.fluid.dygraph as dg
from parakeet.g2p.en import text_to_sequence
from parakeet import audio
from parakeet.models.fastspeech.fastspeech import FastSpeech
from parakeet.models.transformer_tts.utils import *
from parakeet.models.wavenet import WaveNet, UpsampleNet
from parakeet.models.clarinet import STFT, Clarinet, ParallelWaveNet
from parakeet.modules import weight_norm
from parakeet.models.waveflow import WaveFlowModule
from parakeet.utils.layer_tools import freeze
from parakeet.utils import io


def add_config_options_to_parser(parser):
    parser.add_argument("--config", type=str, help="path of the config file")
    parser.add_argument(
        "--vocoder",
        type=str,
        default="griffinlim",
        choices=['griffinlim', 'clarinet', 'waveflow'],
        help="vocoder method")
    parser.add_argument(
        "--config_vocoder", type=str, help="path of the vocoder config file")
    parser.add_argument("--use_gpu", type=int, default=0, help="device to use")
    parser.add_argument(
        "--alpha",
        type=float,
        default=1,
        help="determine the length of the expanded sequence mel, controlling the voice speed."
    )

    parser.add_argument(
        "--checkpoint", type=str, help="fastspeech checkpoint to synthesis")
    parser.add_argument(
        "--checkpoint_vocoder",
        type=str,
        help="vocoder checkpoint to synthesis")

    parser.add_argument(
        "--output",
        type=str,
        default="synthesis",
        help="path to save experiment results")


def synthesis(text_input, args):
    local_rank = dg.parallel.Env().local_rank
    place = (fluid.CUDAPlace(local_rank) if args.use_gpu else fluid.CPUPlace())
    fluid.enable_dygraph(place)

    with open(args.config) as f:
        cfg = yaml.load(f, Loader=yaml.Loader)

    # tensorboard
    if not os.path.exists(args.output):
        os.mkdir(args.output)

    writer = SummaryWriter(os.path.join(args.output, 'log'))

    model = FastSpeech(cfg['network'], num_mels=cfg['audio']['num_mels'])
    # Load parameters.
    global_step = io.load_parameters(
        model=model, checkpoint_path=args.checkpoint)
    model.eval()

    text = np.asarray(text_to_sequence(text_input))
    text = np.expand_dims(text, axis=0)
    pos_text = np.arange(1, text.shape[1] + 1)
    pos_text = np.expand_dims(pos_text, axis=0)

    text = dg.to_variable(text).astype(np.int64)
    pos_text = dg.to_variable(pos_text).astype(np.int64)

    _, mel_output_postnet = model(text, pos_text, alpha=args.alpha)

    if args.vocoder == 'griffinlim':
        #synthesis use griffin-lim
        wav = synthesis_with_griffinlim(
            mel_output_postnet,
            sr=cfg['audio']['sr'],
            n_fft=cfg['audio']['n_fft'],
            num_mels=cfg['audio']['num_mels'],
            power=cfg['audio']['power'],
            hop_length=cfg['audio']['hop_length'],
            win_length=cfg['audio']['win_length'])
    elif args.vocoder == 'clarinet':
        # synthesis use clarinet
        wav = synthesis_with_clarinet(mel_output_postnet, args.config_vocoder,
                                      args.checkpoint_vocoder, place)
    elif args.vocoder == 'waveflow':
        wav = synthesis_with_waveflow(mel_output_postnet, args,
                                      args.checkpoint_vocoder, place)
    else:
        print(
            'vocoder error, we only support griffinlim, clarinet and waveflow, but recevied %s.'
            % args.vocoder)

    writer.add_audio(text_input + '(' + args.vocoder + ')', wav, 0,
                     cfg['audio']['sr'])
    if not os.path.exists(os.path.join(args.output, 'samples')):
        os.mkdir(os.path.join(args.output, 'samples'))
    write(
        os.path.join(
            os.path.join(args.output, 'samples'), args.vocoder + '.wav'),
        cfg['audio']['sr'], wav)
    print("Synthesis completed !!!")
    writer.close()


def synthesis_with_griffinlim(mel_output, sr, n_fft, num_mels, power,
                              hop_length, win_length):
    mel_output = fluid.layers.transpose(
        fluid.layers.squeeze(mel_output, [0]), [1, 0])
    mel_output = np.exp(mel_output.numpy())
    basis = librosa.filters.mel(sr, n_fft, num_mels)
    inv_basis = np.linalg.pinv(basis)
    spec = np.maximum(1e-10, np.dot(inv_basis, mel_output))

    wav = librosa.core.griffinlim(
        spec**power, hop_length=hop_length, win_length=win_length)

    return wav


def synthesis_with_clarinet(mel_output, config_path, checkpoint, place):
    mel_spectrogram = np.exp(mel_output.numpy())
    with open(config_path, 'rt') as f:
        config = yaml.safe_load(f)

    data_config = config["data"]
    n_mels = data_config["n_mels"]

    teacher_config = config["teacher"]
    n_loop = teacher_config["n_loop"]
    n_layer = teacher_config["n_layer"]
    filter_size = teacher_config["filter_size"]

    # only batch=1 for validation is enabled

    fluid.enable_dygraph(place)
    # conditioner(upsampling net)
    conditioner_config = config["conditioner"]
    upsampling_factors = conditioner_config["upsampling_factors"]
    upsample_net = UpsampleNet(upscale_factors=upsampling_factors)
    freeze(upsample_net)

    residual_channels = teacher_config["residual_channels"]
    loss_type = teacher_config["loss_type"]
    output_dim = teacher_config["output_dim"]
    log_scale_min = teacher_config["log_scale_min"]
    assert loss_type == "mog" and output_dim == 3, \
        "the teacher wavenet should be a wavenet with single gaussian output"

    teacher = WaveNet(n_loop, n_layer, residual_channels, output_dim, n_mels,
                      filter_size, loss_type, log_scale_min)
    # load & freeze upsample_net & teacher
    freeze(teacher)

    student_config = config["student"]
    n_loops = student_config["n_loops"]
    n_layers = student_config["n_layers"]
    student_residual_channels = student_config["residual_channels"]
    student_filter_size = student_config["filter_size"]
    student_log_scale_min = student_config["log_scale_min"]
    student = ParallelWaveNet(n_loops, n_layers, student_residual_channels,
                              n_mels, student_filter_size)

    stft_config = config["stft"]
    stft = STFT(
        n_fft=stft_config["n_fft"],
        hop_length=stft_config["hop_length"],
        win_length=stft_config["win_length"])

    lmd = config["loss"]["lmd"]
    model = Clarinet(upsample_net, teacher, student, stft,
                     student_log_scale_min, lmd)
    io.load_parameters(model=model, checkpoint_path=checkpoint)

    if not os.path.exists(args.output):
        os.makedirs(args.output)
    model.eval()

    # Rescale mel_spectrogram.
    min_level, ref_level = 1e-5, 20  # hard code it
    mel_spectrogram = 20 * np.log10(np.maximum(min_level, mel_spectrogram))
    mel_spectrogram = mel_spectrogram - ref_level
    mel_spectrogram = np.clip((mel_spectrogram + 100) / 100, 0, 1)

    mel_spectrogram = dg.to_variable(mel_spectrogram)
    mel_spectrogram = fluid.layers.transpose(mel_spectrogram, [0, 2, 1])

    wav_var = model.synthesis(mel_spectrogram)
    wav_np = wav_var.numpy()[0]

    return wav_np


def synthesis_with_waveflow(mel_output, args, checkpoint, place):
    #mel_output = np.exp(mel_output.numpy())
    mel_output = mel_output.numpy()

    fluid.enable_dygraph(place)
    args.config = args.config_vocoder
    args.use_fp16 = False
    config = io.add_yaml_config_to_args(args)

    mel_spectrogram = dg.to_variable(mel_output)
    mel_spectrogram = fluid.layers.transpose(mel_spectrogram, [0, 2, 1])

    # Build model.
    waveflow = WaveFlowModule(config)
    io.load_parameters(model=waveflow, checkpoint_path=checkpoint)
    for layer in waveflow.sublayers():
        if isinstance(layer, weight_norm.WeightNormWrapper):
            layer.remove_weight_norm()

    # Run model inference.
    wav = waveflow.synthesize(mel_spectrogram, sigma=config.sigma)
    return wav.numpy()[0]


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Synthesis model")
    add_config_options_to_parser(parser)
    args = parser.parse_args()
    pprint(vars(args))
    synthesis("Simple as this proposition is, it is necessary to be stated,",
              args)
