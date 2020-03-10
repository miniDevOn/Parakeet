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
import numpy as np
from collections import namedtuple
from paddle import fluid
import paddle.fluid.dygraph as dg
import paddle.fluid.layers as F
import paddle.fluid.initializer as I

from parakeet.modules.weight_norm import Linear
WindowRange = namedtuple("WindowRange", ["backward", "ahead"])


class Attention(dg.Layer):
    def __init__(self,
                 query_dim,
                 embed_dim,
                 dropout=0.0,
                 window_range=WindowRange(-1, 3),
                 key_projection=True,
                 value_projection=True):
        """Attention Layer for Deep Voice 3.

        Args:
            query_dim (int): the dimension of query vectors. (The size of a single vector of query.)
            embed_dim (int): the dimension of keys and values.
            dropout (float, optional): dropout probability of attention. Defaults to 0.0.
            window_range (WindowRange, optional): range of attention, this is only used at inference. Defaults to WindowRange(-1, 3).
            key_projection (bool, optional): whether the `Attention` Layer has a Linear Layer for the keys to pass through before computing attention. Defaults to True.
            value_projection (bool, optional): whether the `Attention` Layer has a Linear Layer for the values to pass through before computing attention. Defaults to True.
        """
        super(Attention, self).__init__()
        std = np.sqrt(1 / query_dim)
        self.query_proj = Linear(
            query_dim, embed_dim, param_attr=I.Normal(scale=std))
        if key_projection:
            std = np.sqrt(1 / embed_dim)
            self.key_proj = Linear(
                embed_dim, embed_dim, param_attr=I.Normal(scale=std))
        if value_projection:
            std = np.sqrt(1 / embed_dim)
            self.value_proj = Linear(
                embed_dim, embed_dim, param_attr=I.Normal(scale=std))
        std = np.sqrt(1 / embed_dim)
        self.out_proj = Linear(
            embed_dim, query_dim, param_attr=I.Normal(scale=std))

        self.key_projection = key_projection
        self.value_projection = value_projection
        self.dropout = dropout
        self.window_range = window_range

    def forward(self, query, encoder_out, mask=None, last_attended=None):
        """
        Compute contextualized representation and alignment scores.
        
        Args:
            query (Variable): shape(B, T_dec, C_q), dtype float32, the query tensor, where C_q means the query dim.
            encoder_out (keys, values): 
                keys (Variable): shape(B, T_enc, C_emb), dtype float32, the key representation from an encoder, where C_emb means embed dim.
                values (Variable): shape(B, T_enc, C_emb), dtype float32, the value representation from an encoder, where C_emb means embed dim.
            mask (Variable, optional): shape(B, T_enc), dtype float32, mask generated with valid text lengths. Pad tokens corresponds to 1, and valid tokens correspond to 0.
            last_attended (int, optional): The position that received the most attention at last time step. This is only used at inference.

        Outpus:
            x (Variable): shape(B, T_dec, C_q), dtype float32, the contextualized representation from attention mechanism.
            attn_scores (Variable): shape(B, T_dec, T_enc), dtype float32, the alignment tensor, where T_dec means the number of decoder time steps and T_enc means number the number of decoder time steps.
        """
        keys, values = encoder_out
        residual = query
        if self.value_projection:
            values = self.value_proj(values)
        if self.key_projection:
            keys = self.key_proj(keys)
        x = self.query_proj(query)

        x = F.matmul(x, keys, transpose_y=True)

        # mask generated by sentence length
        neg_inf = -1.e30
        if mask is not None:
            neg_inf_mask = F.scale(F.unsqueeze(mask, [1]), neg_inf)
            x += neg_inf_mask

        # if last_attended is provided, focus only on a window range around it
        # to enforce monotonic attention.
        if last_attended is not None:
            locality_mask = np.ones(shape=x.shape, dtype=np.float32)
            backward, ahead = self.window_range
            backward = last_attended + backward
            ahead = last_attended + ahead
            backward = max(backward, 0)
            ahead = min(ahead, x.shape[-1])
            locality_mask[:, :, backward:ahead] = 0.
            locality_mask = dg.to_variable(locality_mask)
            neg_inf_mask = F.scale(locality_mask, neg_inf)
            x += neg_inf_mask

        x = F.softmax(x)
        attn_scores = x
        x = F.dropout(
            x, self.dropout, dropout_implementation="upscale_in_train")
        x = F.matmul(x, values)
        encoder_length = keys.shape[1]

        x = F.scale(x, encoder_length * np.sqrt(1.0 / encoder_length))
        x = self.out_proj(x)
        x = F.scale((x + residual), np.sqrt(0.5))
        return x, attn_scores
