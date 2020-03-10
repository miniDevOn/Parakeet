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

from __future__ import division
import os
import soundfile as sf
from tensorboardX import SummaryWriter
from collections import OrderedDict

from paddle import fluid
import paddle.fluid.dygraph as dg


def make_output_tree(output_dir):
    checkpoint_dir = os.path.join(output_dir, "checkpoints")
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)

    state_dir = os.path.join(output_dir, "states")
    if not os.path.exists(state_dir):
        os.makedirs(state_dir)


def valid_model(model, valid_loader, output_dir, global_step, sample_rate):
    model.eval()
    for i, batch in enumerate(valid_loader):
        # print("sentence {}".format(i))
        path = os.path.join(output_dir,
                            "step_{}_sentence_{}.wav".format(global_step, i))
        audio_clips, mel_specs, audio_starts = batch
        wav_var = model.synthesis(mel_specs)
        wav_np = wav_var.numpy()[0]
        sf.write(path, wav_np, samplerate=sample_rate)
        print("generated {}".format(path))


def eval_model(model, valid_loader, output_dir, sample_rate):
    model.eval()
    for i, batch in enumerate(valid_loader):
        # print("sentence {}".format(i))
        path = os.path.join(output_dir, "sentence_{}.wav".format(i))
        audio_clips, mel_specs, audio_starts = batch
        wav_var = model.synthesis(mel_specs)
        wav_np = wav_var.numpy()[0]
        sf.write(path, wav_np, samplerate=sample_rate)
        print("generated {}".format(path))


def save_checkpoint(model, optim, checkpoint_dir, global_step):
    path = os.path.join(checkpoint_dir, "step_{}".format(global_step))
    dg.save_dygraph(model.state_dict(), path)
    print("saving model to {}".format(path + ".pdparams"))
    if optim:
        dg.save_dygraph(optim.state_dict(), path)
        print("saving optimizer to {}".format(path + ".pdopt"))


def load_model(model, path):
    model_dict, _ = dg.load_dygraph(path)
    model.set_dict(model_dict)
    print("loaded model from {}.pdparams".format(path))


def load_checkpoint(model, optim, path):
    model_dict, optim_dict = dg.load_dygraph(path)
    model.set_dict(model_dict)
    print("loaded model from {}.pdparams".format(path))
    if optim_dict:
        optim.set_dict(optim_dict)
        print("loaded optimizer from {}.pdparams".format(path))


def load_wavenet(model, path):
    wavenet_dict, _ = dg.load_dygraph(path)
    encoder_dict = OrderedDict()
    teacher_dict = OrderedDict()
    for k, v in wavenet_dict.items():
        if k.startswith("encoder."):
            encoder_dict[k.split('.', 1)[1]] = v
        else:
            # k starts with "decoder."
            teacher_dict[k.split('.', 1)[1]] = v

    model.encoder.set_dict(encoder_dict)
    model.teacher.set_dict(teacher_dict)
    print("loaded the encoder part and teacher part from wavenet model.")
