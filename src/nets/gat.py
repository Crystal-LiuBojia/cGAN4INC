"""
Pytorch Geometric
Ref: ttps://github.com/pyg-team/pytorch_geometric/blob/97d55577f1d0bf33c1bfbe0ef864923ad5cb844d/torch_geometric/nn/conv/gat_conv.py
"""

from typing import Union, Tuple, Optional
from torch_geometric.typing import (OptPairTensor, Adj, Size, NoneType,
                                    OptTensor)
import torch
from torch import Tensor
from torch.nn import Parameter
import torch.nn as nn
import torch.nn.functional as F
import math
import scipy
import numpy as np

from torch_scatter import scatter_add
from torch_sparse import SparseTensor, set_diag
from torch_geometric.nn.conv import MessagePassing
from torch_geometric.utils import remove_self_loops, add_self_loops, softmax, to_dense_batch

from torch_geometric.nn.inits import reset, glorot, zeros

class GATConv(MessagePassing):
    r"""The graph attentional operator from the `"Graph Attention Networks"
    <https://arxiv.org/abs/1710.10903>`_ paper
    .. math::
        \mathbf{x}^{\prime}_i = \alpha_{i,i}\mathbf{\Theta}\mathbf{x}_{i} +
        \sum_{j \in \mathcal{N}(i)} \alpha_{i,j}\mathbf{\Theta}\mathbf{x}_{j},
    where the attention coefficients :math:`\alpha_{i,j}` are computed as
    .. math::
        \alpha_{i,j} =
        \frac{
        \exp\left(\mathrm{LeakyReLU}\left(\mathbf{a}^{\top}
        [\mathbf{\Theta}\mathbf{x}_i \, \Vert \, \mathbf{\Theta}\mathbf{x}_j]
        \right)\right)}
        {\sum_{k \in \mathcal{N}(i) \cup \{ i \}}
        \exp\left(\mathrm{LeakyReLU}\left(\mathbf{a}^{\top}
        [\mathbf{\Theta}\mathbf{x}_i \, \Vert \, \mathbf{\Theta}\mathbf{x}_k]
        \right)\right)}.
    Args:
        in_channels (int or tuple): Size of each input sample. A tuple
            corresponds to the sizes of source and target dimensionalities.
        out_channels (int): Size of each output sample.
        heads (int, optional): Number of multi-head-attentions.
            (default: :obj:`1`)
        concat (bool, optional): If set to :obj:`False`, the multi-head
            attentions are averaged instead of concatenated.
            (default: :obj:`True`)
        negative_slope (float, optional): LeakyReLU angle of the negative
            slope. (default: :obj:`0.2`)
        dropout (float, optional): Dropout probability of the normalized
            attention coefficients which exposes each node to a stochastically
            sampled neighborhood during training. (default: :obj:`0`)
        add_self_loops (bool, optional): If set to :obj:`False`, will not add
            self-loops to the input graph. (default: :obj:`True`)
        bias (bool, optional): If set to :obj:`False`, the layer will not learn
            an additive bias. (default: :obj:`True`)
        **kwargs (optional): Additional arguments of
            :class:`torch_geometric.nn.conv.MessagePassing`.
    """
    _alpha: OptTensor

    def __init__(self, in_channels: Union[int, Tuple[int, int]],
                 out_channels: int, heads: int = 1, concat: bool = True,
                 negative_slope: float = 0.2, dropout: float = 0.0,
                 bias: bool = True, **kwargs):
        kwargs.setdefault('aggr', 'add')
        super(GATConv, self).__init__(node_dim=0, **kwargs)

        self.in_channels = in_channels
        self.out_channels = out_channels
        self.heads = heads
        self.concat = concat
        self.negative_slope = negative_slope
        self.dropout = dropout

        if isinstance(in_channels, int):
            self.temp_weight = torch.nn.Linear(in_channels, heads * out_channels, bias=False)
            self.lin_l = self.temp_weight#Linear(in_channels, heads * out_channels, bias=False)
            self.lin_r = self.lin_l
        else:
            self.lin_l = torch.nn.Linear(in_channels[0], heads * out_channels, False)
            self.lin_r = torch.nn.Linear(in_channels[1], heads * out_channels, False)

        self.att_l = Parameter(torch.Tensor(1, heads, out_channels))
        self.att_r = Parameter(torch.Tensor(1, heads, out_channels))

        if bias and concat:
            self.bias = Parameter(torch.Tensor(heads * out_channels))
        elif bias and not concat:
            self.bias = Parameter(torch.Tensor(out_channels))
        else:
            self.register_parameter('bias', None)

        self._alpha = None

        self.reset_parameters()


    def reset_parameters(self):
        glorot(self.lin_l.weight)
        glorot(self.lin_r.weight)
        glorot(self.att_l)
        glorot(self.att_r)
        zeros(self.bias)

    def forward(self, x: Union[Tensor, OptPairTensor], edge_index: Adj,
                size: Size = None, return_attention_weights=None, is_add_self_loops: bool = True):
        r"""
        Args:
            return_attention_weights (bool, optional): If set to :obj:`True`,
                will additionally return the tuple
                :obj:`(edge_index, attention_weights)`, holding the computed
                attention weights for each edge. (default: :obj:`None`)
        """
        H, C = self.heads, self.out_channels
        original_size = edge_index.shape[1]
        x_l: OptTensor = None
        x_r: OptTensor = None
        alpha_l: OptTensor = None
        alpha_r: OptTensor = None

        if isinstance(x, Tensor):
            assert x.dim() == 2, 'Static graphs not supported in `GATConv`.'
            #x_lyy = x_r = self.lin_l(x).view(-1, H, C)
            x = self.lin_l(x) #.view(-1, H, C)
            x_l = x_r = x.view(-1,H,C)

            alpha_l = (x_l * self.att_l).sum(dim=-1)
            alpha_r = (x_r * self.att_r).sum(dim=-1)
        else:
            x_l, x_r = x[0], x[1]
            assert x[0].dim() == 2, 'Static graphs not supported in `GATConv`.'
            x_l = self.lin_l(x_l).view(-1, H, C)
            alpha_l = (x_l * self.att_l).sum(dim=-1)
            if x_r is not None:
                x_r = self.lin_r(x_r).view(-1, H, C)
                alpha_r = (x_r * self.att_r).sum(dim=-1)

        assert x_l is not None
        assert alpha_l is not None

        if is_add_self_loops:
            if isinstance(edge_index, Tensor):
                num_nodes = x_l.size(0)
                if x_r is not None:
                    num_nodes = min(num_nodes, x_r.size(0))
                if size is not None:
                    num_nodes = min(size[0], size[1])
                edge_index, _ = remove_self_loops(edge_index)
                edge_index, _ = add_self_loops(edge_index, num_nodes=num_nodes)
            elif isinstance(edge_index, SparseTensor):
                edge_index = set_diag(edge_index)
        # propagate_type: (x: OptPairTensor, alpha: OptPairTensor)
        out = self.propagate(edge_index, x=(x_l, x_r),
                             alpha=(alpha_l, alpha_r), size=size)

        alpha = self._alpha
        self._alpha = None

        if self.concat:
            out = out.view(-1, self.heads * self.out_channels)
        else:
            out = out.mean(dim=1)

        if self.bias is not None:
            out += self.bias

        if isinstance(return_attention_weights, bool):
            assert alpha is not None
            if isinstance(edge_index, Tensor):
                return out, (edge_index, alpha)
            elif isinstance(edge_index, SparseTensor):
                return out, edge_index.set_value(alpha, layout='coo')
        else:
            return out, edge_index

    def message(self, x_j: Tensor, alpha_j: Tensor, alpha_i: OptTensor,
                index: Tensor, ptr: OptTensor,
                size_i: Optional[int]) -> Tensor:
        alpha = alpha_j if alpha_i is None else alpha_j + alpha_i
        alpha = F.leaky_relu(alpha, self.negative_slope)
        alpha = softmax(alpha, index, ptr, size_i)
        self._alpha = alpha
        alpha = F.dropout(alpha, p=self.dropout, training=self.training)
        return x_j * alpha.unsqueeze(-1)

    def __repr__(self):
        return '{}({}, {}, heads={})'.format(self.__class__.__name__,
                                             self.in_channels,
                                             self.out_channels, self.heads)

class StandGAT1(nn.Module):
    def __init__(self, nfeat, nhid, nclass, dropout,nlayer=1, is_add_self_loops=True):
        super(StandGAT1, self).__init__()
        self.conv1 = GATConv(nfeat, nclass,heads=1)

        self.is_add_self_loops = is_add_self_loops
        self.reg_params = []
        self.non_reg_params = self.conv1.parameters()

    def forward(self, x, adj, edge_weight=None):

        edge_index = adj
        x, edge_index = self.conv1(x,edge_index, is_add_self_loops=self.is_add_self_loops)
        x = F.relu(x)

        return x


class StandGAT2(nn.Module):
    def __init__(self, nfeat, nhid, nclass, dropout,nlayer=2):
        super(StandGAT2, self).__init__()

        num_head = 4
        head_dim = nhid//num_head

        self.conv1 = GATConv(nfeat, head_dim, heads=num_head)
        self.conv2 = GATConv(nhid,  nclass,   heads=1, concat=False)
        self.dropout_p = dropout
        self.is_add_self_loops = True

        self.reg_params = list(self.conv1.parameters())
        self.non_reg_params = self.conv2.parameters()

    def forward(self, x, adj, edge_weight=None):
        edge_index = adj
        x, edge_index = self.conv1(x, edge_index, is_add_self_loops=self.is_add_self_loops)
        x = F.relu(x)
        x = F.dropout(x, p= self.dropout_p, training=self.training)
        x, edge_index = self.conv2(x, edge_index, is_add_self_loops=self.is_add_self_loops)
        return x

class StandGATX(nn.Module):
    def __init__(self, nfeat, nhid, nclass, dropout,nlayer=3):
        super(StandGATX, self).__init__()

        num_head = 4
        head_dim = nhid//num_head

        self.conv1 = GATConv(nfeat, head_dim, heads=num_head)
        self.conv2 = GATConv(nhid, nclass)
        self.convx = nn.ModuleList([GATConv(nhid, head_dim, heads=num_head) for _ in range(nlayer-2)])
        self.dropout_p = dropout
        self.is_add_self_loops = True

        self.reg_params = list(self.conv1.parameters()) + list(self.convx.parameters())
        self.non_reg_params = self.conv2.parameters()


    def forward(self, x, adj, edge_weight=None):
        edge_index = adj
        x, edge_index = self.conv1(x, edge_index, is_add_self_loops=self.is_add_self_loops)
        x = F.relu(x)

        for iter_layer in self.convx:
            x = F.dropout(x, p= self.dropout_p, training=self.training)
            x, edge_index = iter_layer(x, edge_index, is_add_self_loops=self.is_add_self_loops)
            x = F.relu(x)

        x = F.dropout(x,p= self.dropout_p,  training=self.training)
        x, edge_index = self.conv2(x, edge_index,edge_weight, is_add_self_loops=self.is_add_self_loops)

        return x

class StandGATEncoder(nn.Module):
    def __init__(self, nfeat, nhid, nembed, dropout, is_add_self_loops=True):
        super(StandGATEncoder, self).__init__()
        num_head = 8
        head_dim = nhid // num_head
        head_dim_2 = nembed//num_head
        self.conv1 = GATConv(nfeat, head_dim_2, heads=num_head)
        self.conv2 = GATConv(nhid, head_dim_2, heads=num_head)
        self.dropout = dropout

        self.is_add_self_loops = is_add_self_loops


    def forward(self, x, adj, edge_weight=None):

        edge_index = adj
        x, edge_index = self.conv1(x,edge_index, is_add_self_loops=self.is_add_self_loops)
        x = F.dropout(x, self.dropout, training=self.training)
        x = F.elu(x)
        #x, edge_index = self.conv2(x, edge_index, is_add_self_loops=self.is_add_self_loops)
        #x = F.dropout(x, self.dropout, training=self.training)
        #x = F.elu(x)

        return x
class StandGATClssifier(nn.Module):
    def __init__(self, nembed, nhid, nclass, dropout, nlayer=2):
        super(StandGATClssifier, self).__init__()

        num_head = 8
        head_dim = nhid // num_head

        self.conv1 = GATConv(nembed, head_dim, heads=num_head)
        self.conv2 = GATConv(nhid, nclass, heads=1, concat=False)
        self.fakereal = nn.Linear(nhid,2)
        self.dropout_p = dropout
        self.is_add_self_loops = True

        self.reset_parameters()

    def reset_parameters(self):
            nn.init.normal_(self.fakereal.weight, std=0.05)

    def forward(self, x, adj, edge_weight=None):
        edge_index = adj
        x, edge_index = self.conv1(x, edge_index, is_add_self_loops=self.is_add_self_loops)
        x = F.relu(x)
        x = F.dropout(x, p=self.dropout_p, training=self.training)
        logits, edge_index = self.conv2(x, edge_index, is_add_self_loops=self.is_add_self_loops)
        fakeorreal = self.fakereal(x)
        x_class = F.log_softmax(logits, dim=1)  # 这个地方要不要F.elu
        x_fakereal = F.log_softmax(fakeorreal, dim=1)  # 这个地方改成log_softmax还是softmax
        return logits, fakeorreal, x_class, x_fakereal

def create_gat(nfeat, nhid, nclass, dropout, nlayer, nembed=64):
    if nlayer == 1:
        model = StandGAT1(nfeat, nhid, nclass, dropout,nlayer)
    elif nlayer == 2:
        model = StandGAT2(nfeat, nhid, nclass, dropout,nlayer)
    elif nlayer == 3:
        model = StandGATEncoder(nfeat, nhid, nembed, dropout)
    elif nlayer == 4:
        model = StandGATClssifier(nhid, nembed, nclass, dropout)
    return model
