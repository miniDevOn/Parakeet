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
import math
import numpy as np
import paddle.fluid as fluid
import paddle.fluid.dygraph as dg
import paddle.fluid.layers as layers


class Linear(dg.Layer):
    def __init__(self,
                 in_features,
                 out_features,
                 is_bias=True,
                 dtype="float32"):
        super(Linear, self).__init__()
        self.in_features = in_features
        self.out_features = out_features
        self.dtype = dtype
        self.weight = fluid.ParamAttr(
            initializer=fluid.initializer.XavierInitializer())
        self.bias = is_bias

        if is_bias is not False:
            k = math.sqrt(1 / in_features)
            self.bias = fluid.ParamAttr(initializer=fluid.initializer.Uniform(
                low=-k, high=k))

        self.linear = dg.Linear(
            in_features,
            out_features,
            param_attr=self.weight,
            bias_attr=self.bias, )

    def forward(self, x):
        x = self.linear(x)
        return x


class ScaledDotProductAttention(dg.Layer):
    def __init__(self, d_key):
        super(ScaledDotProductAttention, self).__init__()

        self.d_key = d_key

    # please attention this mask is diff from pytorch
    def forward(self,
                key,
                value,
                query,
                mask=None,
                query_mask=None,
                dropout=0.1):
        """
        Scaled Dot Product Attention.
        
        Args:
            key (Variable): Shape(B, T, C), dtype: float32. The input key of attention.
            value (Variable): Shape(B, T, C), dtype: float32. The input value of attention.
            query (Variable): Shape(B, T, C), dtype: float32. The input query of attention.
            mask (Variable): Shape(B, len_q, len_k), dtype: float32. The mask of key.
            query_mask (Variable): Shape(B, len_q, 1), dtype: float32. The mask of query.
            dropout (Constant): dtype: float32. The probability of dropout.
        Returns:
            result (Variable), Shape(B, T, C), the result of mutihead attention.
            attention (Variable), Shape(n_head * B, T, C), the attention of key.
        """
        # Compute attention score
        attention = layers.matmul(
            query, key, transpose_y=True, alpha=self.d_key
            **-0.5)  #transpose the last dim in y

        # Mask key to ignore padding
        if mask is not None:
            attention = attention + mask
        attention = layers.softmax(attention)
        attention = layers.dropout(
            attention, dropout, dropout_implementation='upscale_in_train')

        # Mask query to ignore padding
        if query_mask is not None:
            attention = attention * query_mask

        result = layers.matmul(attention, value)
        return result, attention


class MultiheadAttention(dg.Layer):
    def __init__(self,
                 num_hidden,
                 d_k,
                 d_q,
                 num_head=4,
                 is_bias=False,
                 dropout=0.1,
                 is_concat=True):
        super(MultiheadAttention, self).__init__()
        self.num_hidden = num_hidden
        self.num_head = num_head
        self.d_k = d_k
        self.d_q = d_q
        self.dropout = dropout
        self.is_concat = is_concat

        self.key = Linear(num_hidden, num_head * d_k, is_bias=is_bias)
        self.value = Linear(num_hidden, num_head * d_k, is_bias=is_bias)
        self.query = Linear(num_hidden, num_head * d_q, is_bias=is_bias)

        self.scal_attn = ScaledDotProductAttention(d_k)

        if self.is_concat:
            self.fc = Linear(num_head * d_q * 2, num_hidden)
        else:
            self.fc = Linear(num_head * d_q, num_hidden)

        self.layer_norm = dg.LayerNorm(num_hidden)

    def forward(self, key, value, query_input, mask=None, query_mask=None):
        """
        Multihead Attention.
        
        Args:
            key (Variable): Shape(B, T, C), dtype: float32. The input key of attention.
            value (Variable): Shape(B, T, C), dtype: float32. The input value of attention.
            query_input (Variable): Shape(B, T, C), dtype: float32. The input query of attention.
            mask (Variable): Shape(B, len_q, len_k), dtype: float32. The mask of key.
            query_mask (Variable): Shape(B, len_q, 1), dtype: float32. The mask of query.
        Returns:
            result (Variable), Shape(B, T, C), the result of mutihead attention.
            attention (Variable), Shape(n_head * B, T, C), the attention of key.
        """

        batch_size = key.shape[0]
        seq_len_key = key.shape[1]
        seq_len_query = query_input.shape[1]

        # Make multihead attention
        # key & value.shape = (batch_size, seq_len, feature)(feature = num_head * num_hidden_per_attn)
        key = layers.reshape(
            self.key(key), [batch_size, seq_len_key, self.num_head, self.d_k])
        value = layers.reshape(
            self.value(value),
            [batch_size, seq_len_key, self.num_head, self.d_k])
        query = layers.reshape(
            self.query(query_input),
            [batch_size, seq_len_query, self.num_head, self.d_q])

        key = layers.reshape(
            layers.transpose(key, [2, 0, 1, 3]), [-1, seq_len_key, self.d_k])
        value = layers.reshape(
            layers.transpose(value, [2, 0, 1, 3]),
            [-1, seq_len_key, self.d_k])
        query = layers.reshape(
            layers.transpose(query, [2, 0, 1, 3]),
            [-1, seq_len_query, self.d_q])

        result, attention = self.scal_attn(
            key, value, query, mask=mask, query_mask=query_mask)

        key = layers.reshape(
            layers.transpose(key, [2, 0, 1, 3]), [-1, seq_len_key, self.d_k])
        value = layers.reshape(
            layers.transpose(value, [2, 0, 1, 3]),
            [-1, seq_len_key, self.d_k])
        query = layers.reshape(
            layers.transpose(query, [2, 0, 1, 3]),
            [-1, seq_len_query, self.d_q])

        result, attention = self.scal_attn(
            key, value, query, mask=mask, query_mask=query_mask)

        # concat all multihead result
        result = layers.reshape(
            result, [self.num_head, batch_size, seq_len_query, self.d_q])
        result = layers.reshape(
            layers.transpose(result, [1, 2, 0, 3]),
            [batch_size, seq_len_query, -1])
        if self.is_concat:
            result = layers.concat([query_input, result], axis=-1)
        result = layers.dropout(
            self.fc(result),
            self.dropout,
            dropout_implementation='upscale_in_train')
        result = result + query_input

        result = self.layer_norm(result)
        return result, attention
