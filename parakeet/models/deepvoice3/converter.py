import numpy as np
from itertools import chain

import paddle.fluid.layers as F
import paddle.fluid.initializer as I
import paddle.fluid.dygraph as dg

from parakeet.modules.weight_norm import Conv1D, Conv1DTranspose, Conv2D, Conv2DTranspose, Linear
from parakeet.models.deepvoice3.conv1dglu import Conv1DGLU
from parakeet.models.deepvoice3.encoder import ConvSpec


def upsampling_4x_blocks(n_speakers, speaker_dim, target_channels, dropout):
    # upsampling convolitions
    upsampling_convolutions = [
        Conv1DTranspose(target_channels,
                        target_channels,
                        2,
                        stride=2,
                        param_attr=I.Normal(np.sqrt(1 / target_channels))),
        Conv1DGLU(n_speakers,
                  speaker_dim,
                  target_channels,
                  target_channels,
                  3,
                  dilation=1,
                  std_mul=1.,
                  dropout=dropout),
        Conv1DGLU(n_speakers,
                  speaker_dim,
                  target_channels,
                  target_channels,
                  3,
                  dilation=3,
                  std_mul=4.,
                  dropout=dropout),
        Conv1DTranspose(target_channels,
                        target_channels,
                        2,
                        stride=2,
                        param_attr=I.Normal(scale=np.sqrt(4. /
                                                          target_channels))),
        Conv1DGLU(n_speakers,
                  speaker_dim,
                  target_channels,
                  target_channels,
                  3,
                  dilation=1,
                  std_mul=1.,
                  dropout=dropout),
        Conv1DGLU(n_speakers,
                  speaker_dim,
                  target_channels,
                  target_channels,
                  3,
                  dilation=3,
                  std_mul=4.,
                  dropout=dropout)
    ]
    return upsampling_convolutions


def upsampling_2x_blocks(n_speakers, speaker_dim, target_channels, dropout):
    upsampling_convolutions = [
        Conv1DTranspose(target_channels,
                        target_channels,
                        2,
                        stride=2,
                        param_attr=I.Normal(scale=np.sqrt(1. /
                                                          target_channels))),
        Conv1DGLU(n_speakers,
                  speaker_dim,
                  target_channels,
                  target_channels,
                  3,
                  dilation=1,
                  std_mul=1.,
                  dropout=dropout),
        Conv1DGLU(n_speakers,
                  speaker_dim,
                  target_channels,
                  target_channels,
                  3,
                  dilation=3,
                  std_mul=4.,
                  dropout=dropout)
    ]
    return upsampling_convolutions


def upsampling_1x_blocks(n_speakers, speaker_dim, target_channels, dropout):
    upsampling_convolutions = [
        Conv1DGLU(n_speakers,
                  speaker_dim,
                  target_channels,
                  target_channels,
                  3,
                  dilation=3,
                  std_mul=4.,
                  dropout=dropout)
    ]
    return upsampling_convolutions


class Converter(dg.Layer):
    """
    Vocoder that transforms mel spectrogram (or ecoder hidden states) 
    to waveform.
    """
    def __init__(self,
                 n_speakers,
                 speaker_dim,
                 in_channels,
                 linear_dim,
                 convolutions=(ConvSpec(256, 5, 1), ) * 4,
                 time_upsampling=1,
                 dropout=0.0):
        super(Converter, self).__init__()

        self.n_speakers = n_speakers
        self.speaker_dim = speaker_dim
        self.in_channels = in_channels
        self.linear_dim = linear_dim
        # CAUTION: this should equals the downsampling steps coefficient
        self.time_upsampling = time_upsampling
        self.dropout = dropout

        target_channels = convolutions[0].out_channels

        # conv proj to target channels
        self.first_conv_proj = Conv1D(
            in_channels,
            target_channels,
            1,
            param_attr=I.Normal(scale=np.sqrt(1 / in_channels)))

        # Idea from nyanko
        if time_upsampling == 4:
            self.upsampling_convolutions = dg.LayerList(
                upsampling_4x_blocks(n_speakers, speaker_dim, target_channels,
                                     dropout))
        elif time_upsampling == 2:
            self.upsampling_convolutions = dg.LayerList(
                upsampling_2x_blocks(n_speakers, speaker_dim, target_channels,
                                     dropout))
        elif time_upsampling == 1:
            self.upsampling_convolutions = dg.LayerList(
                upsampling_1x_blocks(n_speakers, speaker_dim, target_channels,
                                     dropout))
        else:
            raise ValueError(
                "Upsampling factors other than {1, 2, 4} are Not supported.")

        # post conv layers
        std_mul = 4.0
        in_channels = target_channels
        self.convolutions = dg.LayerList()
        for (out_channels, filter_size, dilation) in convolutions:
            if in_channels != out_channels:
                std = np.sqrt(std_mul / in_channels)
                # CAUTION: relu
                self.convolutions.append(
                    Conv1D(in_channels,
                           out_channels,
                           1,
                           act="relu",
                           param_attr=I.Normal(scale=std)))
                in_channels = out_channels
                std_mul = 2.0
            self.convolutions.append(
                Conv1DGLU(n_speakers,
                          speaker_dim,
                          in_channels,
                          out_channels,
                          filter_size,
                          dilation=dilation,
                          std_mul=std_mul,
                          dropout=dropout))
            in_channels = out_channels
            std_mul = 4.0

        # final conv proj, channel transformed to linear dim
        std = np.sqrt(std_mul * (1 - dropout) / in_channels)
        # CAUTION: sigmoid
        self.last_conv_proj = Conv1D(in_channels,
                                     linear_dim,
                                     1,
                                     act="sigmoid",
                                     param_attr=I.Normal(scale=std))

    def forward(self, x, speaker_embed=None):
        """
        Convert mel spectrogram or decoder hidden states to linear spectrogram.
        
        Args:
            x (Variable): Shape(B, T_mel, C_in), converter inputs, where
                C_in means the input channel for the converter. Note that it 
                can be either C_mel (channel of mel spectrogram) or C_dec // r.
                When use mel_spectrogram as the input of converter, C_in = 
                C_mel; and when use decoder states as the input of converter,
                C_in = C_dec // r. In this scenario, decoder hidden states are
                treated as if they were r outputs per decoder step and are
                unpacked before passing to the converter.
            speaker_embed (Variable, optional): shape(B, C_sp), speaker
                embedding, where C_sp means the speaker embedding size.

        Returns:
            out (Variable): Shape(B, T_lin, C_lin), the output linear 
                spectrogram, where C_lin means the channel of linear 
                spectrogram and T_linear means the length(time steps) of linear
                spectrogram. T_line = time_upsampling * T_mel, which depends 
                on the time_upsampling converter.
        """
        x = F.transpose(x, [0, 2, 1])
        x = self.first_conv_proj(x)

        if speaker_embed is not None:
            speaker_embed = F.dropout(
                speaker_embed,
                self.dropout,
                dropout_implementation="upscale_in_train")

        for layer in chain(self.upsampling_convolutions, self.convolutions):
            if isinstance(layer, Conv1DGLU):
                x = layer(x, speaker_embed)
            else:
                x = layer(x)

        out = self.last_conv_proj(x)
        out = F.transpose(out, [0, 2, 1])
        return out