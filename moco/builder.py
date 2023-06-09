# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
import torch
import torch.nn as nn
from functools import partial

# SplitBatchNorm: simulate multi-gpu behavior of BatchNorm in one gpu by splitting alone the batch dimension
# implementation adapted from https://github.com/davidcpage/cifar10-fast/blob/master/torch_backend.py
class SplitBatchNorm(nn.BatchNorm2d):
    def __init__(self, num_features, num_splits, **kw):
        super().__init__(num_features, **kw)
        self.num_splits = num_splits

    def forward(self, input):
        N, C, H, W = input.shape
        if self.training or not self.track_running_stats:
            running_mean_split = self.running_mean.repeat(self.num_splits)
            running_var_split = self.running_var.repeat(self.num_splits)
            outcome = nn.functional.batch_norm(
                input.view(-1, C * self.num_splits, H, W), running_mean_split, running_var_split,
                self.weight.repeat(self.num_splits), self.bias.repeat(self.num_splits),
                True, self.momentum, self.eps).view(N, C, H, W)
            self.running_mean.data.copy_(running_mean_split.view(self.num_splits, C).mean(dim=0))
            self.running_var.data.copy_(running_var_split.view(self.num_splits, C).mean(dim=0))
            return outcome
        else:
            return nn.functional.batch_norm(
                input, self.running_mean, self.running_var,
                self.weight, self.bias, False, self.momentum, self.eps)

class MoCo(nn.Module):
    """
    Build a MoCo model with: a query encoder, a key encoder, and a queue
    https://arxiv.org/abs/1911.05722
    """
    def __init__(self, base_encoder, dim=128, K=65536, m=0.999, T=0.07, mlp=False, bands='all'):
        """
        dim: feature dimension (default: 128)
        K: queue size; number of negative keys (default: 65536)
        m: moco momentum of updating key encoder (default: 0.999)
        T: softmax temperature (default: 0.07)
        """
        super(MoCo, self).__init__()

        self.K = K
        self.m = m
        self.T = T

        # create the encoders
        # num_classes is the output fc dimension
        norm_layer = partial(SplitBatchNorm, num_splits=16) # split batchnorm
        self.encoder_q = base_encoder(num_classes=dim, norm_layer=norm_layer)
        self.encoder_k = base_encoder(num_classes=dim, norm_layer=norm_layer)

        if bands=='B12':
            self.encoder_q.conv1 = torch.nn.Conv2d(12,64,kernel_size=(7,7),stride=(2,2),padding=(3,3),bias=False)
            self.encoder_k.conv1 = torch.nn.Conv2d(12,64,kernel_size=(7,7),stride=(2,2),padding=(3,3),bias=False)
        elif bands=='B13':
            #self.encoder_q.conv1 = torch.nn.Conv2d(13,64,kernel_size=(3,3),stride=(1,1),padding=(1,1),bias=False)
            #self.encoder_k.conv1 = torch.nn.Conv2d(13,64,kernel_size=(3,3),stride=(1,1),padding=(1,1),bias=False)
            self.encoder_q.conv1 = torch.nn.Conv2d(13,64,kernel_size=(7,7),stride=(2,2),padding=(3,3),bias=False)
            self.encoder_k.conv1 = torch.nn.Conv2d(13,64,kernel_size=(7,7),stride=(2,2),padding=(3,3),bias=False)
        elif bands=='B15':
            #self.encoder_q.conv1 = torch.nn.Conv2d(13,64,kernel_size=(3,3),stride=(1,1),padding=(1,1),bias=False)
            #self.encoder_k.conv1 = torch.nn.Conv2d(13,64,kernel_size=(3,3),stride=(1,1),padding=(1,1),bias=False)
            self.encoder_q.conv1 = torch.nn.Conv2d(15,64,kernel_size=(7,7),stride=(2,2),padding=(3,3),bias=False)
            self.encoder_k.conv1 = torch.nn.Conv2d(15,64,kernel_size=(7,7),stride=(2,2),padding=(3,3),bias=False)
        elif bands=='B2':
            #self.encoder_q.conv1 = torch.nn.Conv2d(13,64,kernel_size=(3,3),stride=(1,1),padding=(1,1),bias=False)
            #self.encoder_k.conv1 = torch.nn.Conv2d(13,64,kernel_size=(3,3),stride=(1,1),padding=(1,1),bias=False)
            self.encoder_q.conv1 = torch.nn.Conv2d(2,64,kernel_size=(7,7),stride=(2,2),padding=(3,3),bias=False)
            self.encoder_k.conv1 = torch.nn.Conv2d(2,64,kernel_size=(7,7),stride=(2,2),padding=(3,3),bias=False)
            
            
            #self.encoder_q.maxpool = torch.nn.Identity()
            #self.encoder_k.maxpool = torch.nn.Identity()

        if mlp:  # hack: brute-force replacement
            dim_mlp = self.encoder_q.fc.weight.shape[1]
            self.encoder_q.fc = nn.Sequential(nn.Linear(dim_mlp, dim_mlp), nn.ReLU(), self.encoder_q.fc)
            self.encoder_k.fc = nn.Sequential(nn.Linear(dim_mlp, dim_mlp), nn.ReLU(), self.encoder_k.fc)

        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            param_k.data.copy_(param_q.data)  # initialize
            param_k.requires_grad = False  # not update by gradient

        # create the queue
        self.register_buffer("queue", torch.randn(dim, K))
        self.queue = nn.functional.normalize(self.queue, dim=0)

        self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))

    @torch.no_grad()
    def _momentum_update_key_encoder(self):
        """
        Momentum update of the key encoder
        """
        for param_q, param_k in zip(self.encoder_q.parameters(), self.encoder_k.parameters()):
            param_k.data = param_k.data * self.m + param_q.data * (1. - self.m)

    @torch.no_grad()
    def _dequeue_and_enqueue_single_gpu(self, keys):
        # gather keys before updating queue
        #keys = concat_all_gather(keys)

        batch_size = keys.shape[0]

        ptr = int(self.queue_ptr)
        assert self.K % batch_size == 0  # for simplicity

        # replace the keys at ptr (dequeue and enqueue)
        self.queue[:, ptr:ptr + batch_size] = keys.T
        ptr = (ptr + batch_size) % self.K  # move pointer

        self.queue_ptr[0] = ptr

    @torch.no_grad()
    def _batch_shuffle_single_gpu(self, x):
        """
        Batch shuffle, for making use of BatchNorm.
        """

        # random shuffle index
        idx_shuffle = torch.randperm(x.shape[0]).cuda()

        # index for restoring
        idx_unshuffle = torch.argsort(idx_shuffle)


        return x[idx_shuffle], idx_unshuffle

    @torch.no_grad()
    def _batch_unshuffle_single_gpu(self, x, idx_unshuffle):
        """
        Undo batch shuffle.
        """

        return x[idx_unshuffle]

    def forward(self, im_q, im_k):
        """
        Input:
            im_q: a batch of query images
            im_k: a batch of key images
        Output:
            logits, targets
        """

        # compute query features
        q = self.encoder_q(im_q)  # queries: NxC
        q = nn.functional.normalize(q, dim=1)

        # compute key features
        with torch.no_grad():  # no gradient to keys
            self._momentum_update_key_encoder()  # update the key encoder

            # shuffle for making use of BN
            im_k, idx_unshuffle = self._batch_shuffle_single_gpu(im_k)

            k = self.encoder_k(im_k)  # keys: NxC
            k = nn.functional.normalize(k, dim=1)

            # undo shuffle
            k = self._batch_unshuffle_single_gpu(k, idx_unshuffle)

        # compute logits
        # Einstein sum is more intuitive
        # positive logits: Nx1
        l_pos = torch.einsum('nc,nc->n', [q, k]).unsqueeze(-1)
        # negative logits: NxK
        l_neg = torch.einsum('nc,ck->nk', [q, self.queue.clone().detach()])

        # logits: Nx(1+K)
        logits = torch.cat([l_pos, l_neg], dim=1)

        # apply temperature
        logits /= self.T

        # labels: positive key indicators
        labels = torch.zeros(logits.shape[0], dtype=torch.long).cuda()

        # dequeue and enqueue
        self._dequeue_and_enqueue_single_gpu(k)

        return logits, labels


# utils
@torch.no_grad()
def concat_all_gather(tensor):
    """
    Performs all_gather operation on the provided tensors.
    *** Warning ***: torch.distributed.all_gather has no gradient.
    """
    tensors_gather = [torch.ones_like(tensor)
        for _ in range(torch.distributed.get_world_size())]
    torch.distributed.all_gather(tensors_gather, tensor, async_op=False)

    output = torch.cat(tensors_gather, dim=0)
    return output
