# Copyright (c) 2021 PaddlePaddle Authors. All Rights Reserved.
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

from collections import defaultdict
from typing import Optional, Callable, Dict

from tqdm import tqdm
import paddle
from paddle import Tensor
from paddle.nn import Layer
from paddle.io import DataLoader

from parakeet.training.reporter import scope, report, DictSummary


class StandardEvaluator(object):
    def __init__(self, model: Layer, dataloader: DataLoader):
        # it is designed to hold multiple models
        models = {"main": model}
        self.models: Dict[str, Layer] = models
        self.model = model

        # dataloaders
        self.dataloader = dataloader

    def evaluate_core(self, batch):
        # compute
        self.model(batch)  # you may report here

    def evaluate(self):
        # switch to eval mode
        for layer in self.models.values():
            layer.eval()

        summary = DictSummary()
        for batch in self.dataloader:
            observation = {}
            with scope(observation):
                with paddle.no_grad():
                    self.evaluate_core(
                        batch)  # main evaluation computation here.
            summary.add(observation)
        summary = summary.compute_mean()
        return summary

    def __call__(self, trainer=None):
        self.observation = {}
        with scope(self.observation):
            summary = self.evaluate()
            for k, v in summary.items():
                report(k, v)
        print(self.observation)
