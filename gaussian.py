import torch
from torch.nn import Parameter
from torch import FloatTensor, LongTensor

import abc, itertools, math, types
from numpy import prod

import torch.nn.functional as F


from util import *
import util

import sys
import time, random, logging

from enum import Enum

from tqdm import trange

# added to the sigmas to prevent NaN
EPSILON = 10e-7
PROPER_SAMPLING = False # NB: set to true for very small tranformations.
BATCH_NEIGHBORS = True
SIGMA_BOOST = 2.0

"""
Original version of the hyperlayer, which takes samples for a particular index tuple, and only distributes the value
over the sample belonging to that tuple. This is in constrast to the hyperlayer in globalsampling, which distributes all
values over all samples.

This version is not used in any of the experiments in the paper.
"""

class Bias(Enum):
    """

    """
    # No bias is used.`c
    NONE = 1

    # The bias is returned as a single dense tensor of floats.
    DENSE = 2

    # The bias is returned in sparse format, in the same way as the weight matrix is.
    SPARSE = 3

def flatten(input):
    return input.view(input.size(0), -1)

def fi_matrix(indices, shape):
    batchsize, rows, rank = indices.size()

    prod = torch.LongTensor(rank).fill_(1)

    if indices.is_cuda:
        prod = prod.cuda()

    for i in range(rank):
        prod[i] = 1
        for j in range(i + 1, len(shape)):
            prod[i] *= shape[j]

    indices = indices * prod.unsqueeze(0).unsqueeze(0).expand_as(indices)

    return indices.sum(dim=2)

def fi(indices, shape, use_cuda=False):
    """
    Returns the single index of the entry indicated by the given index-tuple, after a tensor (of the given shape) is
    flattened into a vector (by t.view(-1))

    :param indices:
    :param shape:
    :return:
    """
    assert indices.is_cuda == use_cuda

    batchsize, rank = indices.size()

    res = torch.cuda.LongTensor(batchsize).fill_(0) if use_cuda else LongTensor(batchsize).fill_(0)

    for i in range(rank):
        prod = torch.cuda.LongTensor(batchsize).fill_(1) if use_cuda else LongTensor(batchsize).fill_(1)

        for j in range(i + 1, len(shape)):
            prod *= shape[j]

        res += prod * indices[:, i]

    return res

def tup(index, shape, use_cuda=False):
    """
    Returns the tuple indicated by a given single integer (reverse of fi(...)).
    :param indices:
    :param shape:
    :param use_cuda:
    :return:
    """

    num,  = index.size()

    result = torch.cuda.LongTensor(num, len(shape)) if use_cuda else LongTensor(num, len(shape))

    for dim in range(len(shape) - 1):
        per_inc = util.prod(shape[dim+1:])
        result[:, dim] = index / per_inc
        index = index % per_inc
    result[:, -1] = index

    return result


def prod(tuple):
    result = 1

    for v in tuple:
        result *= v

    return result

# from memory_profiler import profile
# @profile
def flatten_indices(indices, in_shape, out_shape, use_cuda=False):
    """
    Turns a n NxK matrix of N index-tuples for a tensor T of rank K into an Nx2 matrix M of index-tuples for a _matrix_
    that is created by flattening the first 'in_shape' dimensions into the vertical dimension of M and the remaining
    dimensions in the the horizontal dimension of M.

    :param indices: Long tensor
    :param in_rank:
    :return: A matrix of size N by 2. The size of the matrix as a LongTensor
    """

    batchsize, n, rank = indices.size()

    inrank = len(in_shape)
    outrank = len(out_shape)

    result = torch.cuda.LongTensor(batchsize, n, 2) if use_cuda else LongTensor(batchsize, n, 2)

    for row in range(n):
        result[:, row, 0] = fi(indices[:, row, 0:outrank], out_shape, use_cuda)   # i index of the weight matrix
        result[:, row, 1] = fi(indices[:, row, outrank:rank], in_shape, use_cuda) # j index

    return result, LongTensor((prod(out_shape), prod(in_shape)))

def flatten_indices_mat(indices, in_shape, out_shape):
    """
    Turns a n NxK matrix of N index-tuples for a tensor T of rank K into an Nx2 matrix M of index-tuples for a _matrix_
    that is created by flattening the first 'in_shape' dimensions into the vertical dimension of M and the remaining
    dimensions in the the horizontal dimension of M.

    :param indices: Long tensor
    :param in_rank:
    :return: (1) A matrix of size N by 2, (2) the dimensions of M
    """

    batchsize, n, rank = indices.size()

    inrank = len(in_shape)
    outrank = len(out_shape)

    result = torch.cuda.LongTensor(batchsize, n, 2) if indices.is_cuda else LongTensor(batchsize, n, 2)

    left = fi_matrix(indices[:, :, 0:outrank], out_shape)   # i index of the weight matrix
    right = fi_matrix(indices[:, :, outrank:rank], in_shape) # j index

    result = torch.cat([left.unsqueeze(2), right.unsqueeze(2)], dim=2)

    return result, LongTensor((prod(out_shape), prod(in_shape)))


def sort(indices, vals, use_cuda=False):
    """

    :param indices:
    :return:
    """
    batchsize, n, _ = indices.size()

    inew = torch.cuda.LongTensor(indices.size()) if use_cuda else LongTensor(indices.size())
    vnew = torch.cuda.FloatTensor(vals.size()) if use_cuda else FloatTensor(vals.size())

    vnew = Variable(vnew)

    for b in range(batchsize):

        _, ixs = torch.sort(indices[b, :, 0])

        inew[b, :, :] = indices[b, :, :][ixs]
        vnew[b, :] = vals[b, :][ixs]

    return inew, vnew

def densities(points, means, sigmas):
    """
    Compute the unnormalized PDFs of the points under the given MVNs

    (with sigma a diagonal matrix per MVN)

    :param means:
    :param sigmas:
    :param points:
    :return:
    """

    # n: number of MVNs
    # d: number of points per MVN
    # rank: dim of points

    batchsize, n, d, rank = points.size()

    means = means.unsqueeze(2).expand_as(points)

    sigmas = sigmas.unsqueeze(2).expand_as(points)
    sigmas_squared = torch.sqrt(1.0/(EPSILON+sigmas))

    points = points - means
    points = points * sigmas_squared

    # Compute dot products for all points
    # -- unroll the batch/n dimensions
    points = points.view(-1, 1, rank, 1).squeeze(3)
    # -- dot prod
    products = torch.bmm(points, points.transpose(1,2))
    # -- reconstruct shape
    products = products.view(batchsize, n, d)

    num = torch.exp(- 0.5 * products)

    return num

def densities_single(points, means, sigmas):
    """
    Compute the unnormalized PDFs of the points under the given MVNs

    (with sigma a single number per MVN)

    :param means:
    :param sigmas:
    :param points:
    :return:
    """

    # n: number of MVNs
    # d: number of points per MVN
    # rank: dim of points

    batchsize, n, d, rank = points.size()

    means = means.unsqueeze(2).expand_as(points)

    points = points - means

    # Compute dot products for all points
    # -- unroll the batch/n dimensions
    points = points.view(-1, 1, rank, 1).squeeze(3)
    # -- dot prod
    products = torch.bmm(points, points.transpose(1,2))
    # -- reconstruct shape
    products = products.view(batchsize, n, d)

    sigmas = sigmas.unsqueeze(2).expand_as(products)
    sigmas = torch.pow(sigmas, 2)

    num = torch.exp(- products * (1.0/(2.0*(sigmas + EPSILON))))

    return num

def sample_indices(batchsize, k, num, rng, use_cuda=False):
    """
    Sample 'num' integer indices within the dimensions indicated by the tuple, resulting in a LongTensor of size
        batchsize x k x num x len(rng).
    Ensures that for fixed values of the first and second dimension, no tuple is sample twice.

    :return:
    """

    total = util.prod(rng)
    num_means = batchsize * k

    # First, we'll sample some flat indices, and then compute their corresponding index-tuples
    flat_indices = LongTensor(num_means, num)
    for row in range(num_means):
        lst = random.sample(range(total), num)
        flat_indices[row, :] = LongTensor(lst)

    flat_indices = flat_indices.view(-1)
    full_indices = tup(flat_indices,rng, use_cuda)

    # Reshape
    full_indices = full_indices.unsqueeze(0).unsqueeze(0).view(batchsize, k, num, len(rng))

    if use_cuda:
        full_indices = full_indices.cuda()

    return full_indices

class HyperLayer(nn.Module):
    """
        Abstract class for the hyperlayer. Implement by defining a hypernetwork, and returning it from the hyper() method.
    """
    @abc.abstractmethod
    def hyper(self, input):
        """
            Applies the hypernetwork. This network should take the same input as the hyperlayer itself
            and output a pair (L, V), with L a matrix of k by R (with R the rank of W) and a vector V of length k.
        """
        return

    def cuda(self, device_id=None):

        self.use_cuda = True
        super().cuda(device_id)

        self.floor_mask = self.floor_mask.cuda()

    def __init__(self,
                 in_rank, out_shape, additional=0, bias_type=Bias.DENSE, sparse_input=False,
                 subsample=None, reinforce=False, relative_range=None, rr_additional=None):
        super().__init__()

        self.reinforce = reinforce
        self.use_cuda = False
        self.in_rank = in_rank
        self.out_size = out_shape # without batch dimension
        self.additional = additional
        self.relative_range = relative_range
        self.rr_additional = rr_additional

        self.weights_rank = in_rank + len(out_shape) # implied rank of W

        self.bias_type = bias_type
        self.sparse_input = sparse_input
        self.subsample = subsample

        # create a tensor with all binary sequences of length 'rank' as rows
        lsts = [[int(b) for b in bools] for bools in itertools.product([True, False], repeat=self.weights_rank)]
        self.floor_mask = torch.ByteTensor(lsts)

    def bmult(self, width, height, num_indices, batchsize, use_cuda):

        bmult = torch.cuda.LongTensor([height, width]) if use_cuda else LongTensor([height, width])
        m = torch.cuda.LongTensor(range(batchsize)) if use_cuda else LongTensor(range(batchsize))

        bmult = bmult.unsqueeze(0).unsqueeze(0)
        m = m.unsqueeze(1).unsqueeze(1)

        bmult = bmult.expand(batchsize, num_indices, 2)
        m = m.expand(batchsize, num_indices, 2)

        return m * bmult

    def sigma_loss(self, input):
        """
        Possible regularization loss term: sigmoid(-log(mean sigma))

        Adding this to the loss will keep the sigmas from converging too quickly (due to undersampling)

        :param input:
        :return:
        """
        sigmas = self.hyper(input)[1]

        return  - torch.log(sigmas.sum(dim=1)/ self.k).sum()

    def split_out(self, res, input_size, output_size):
        """
        Utility function. res is a B x K x Wrank+2 tensor with range from
        -inf to inf, this function splits out the means, sigmas and values, and
        applies the required activations.

        :param res:
        :param input_size:
        :param output_size:
        :param gain:
        :return:
        """

        b, k, width = res.size()
        w_rank = width - 2

        means = nn.functional.sigmoid(res[:, :, 0:w_rank])
        means = means.unsqueeze(2).contiguous().view(-1, k, w_rank)

        ## expand the indices to the range [0, max]

        # Limits for each of the w_rank indices
        # and scales for the sigmas
        ws = list(output_size) + list(input_size)
        s = torch.cuda.FloatTensor(ws) if self.use_cuda else FloatTensor(ws)
        s = Variable(s.contiguous())

        ss = s.unsqueeze(0).unsqueeze(0)
        sm = s - 1
        sm = sm.unsqueeze(0).unsqueeze(0)

        means = means * sm.expand_as(means)

        sigmas = nn.functional.softplus(res[:, :, w_rank:w_rank + 1] + SIGMA_BOOST).squeeze(2) + EPSILON

        values = res[:, :, w_rank + 1:].squeeze(2)

        self.last_sigmas = sigmas.data
        self.last_values = values.data

        sigmas = sigmas.unsqueeze(2).expand_as(means)
        sigmas = sigmas * ss.expand_as(sigmas)

        return means, sigmas, values


    def split_shared(self, res, input_size, output_size, values):
        """
        Splits res into means and sigmas, samples values according to multinomial parameters
        in res

        :param res:
        :param input_size:
        :param output_size:
        :param gain:
        :return:
        """

        b, k, width = res.size()
        w_rank = len(input_size) + len(output_size)

        means = nn.functional.sigmoid(res[:, :, 0:w_rank])
        means = means.unsqueeze(2).contiguous().view(-1, k, w_rank)

        ## expand the indices to the range [0, max]

        # Limits for each of the w_rank indices
        # and scales for the sigmas
        ws = list(output_size) + list(input_size)
        s = torch.cuda.FloatTensor(ws) if self.use_cuda else FloatTensor(ws)
        s = Variable(s.contiguous())

        ss = s.unsqueeze(0).unsqueeze(0)
        sm = s - 1
        sm = sm.unsqueeze(0).unsqueeze(0)

        means = means * sm.expand_as(means)

        sigmas = nn.functional.softplus(res[:, :, w_rank:w_rank+1]) + EPSILON

        sigmas = sigmas.expand_as(means)
        sigmas = sigmas * ss.expand_as(sigmas)

        # extract the values
        vweights = res[:, :, w_rank+1:].contiguous()

        assert vweights.size()[2] == values.size()[0]

        vweights = util.bsoftmax(vweights) + EPSILON

        samples, snode = util.bmultinomial(vweights, num_samples=1)

        weights = values[samples.data.view(-1)].view(b, k)

        return means, sigmas, weights, snode

    def discretize(self, means, sigmas, values, rng=None, additional=16, use_cuda=False, relative_range=None):
        """
        Takes the output of a hypernetwork (real-valued indices and corresponding values) and turns it into a list of
        integer indices, by "distributing" the values to the nearest neighboring integer indices.

        NB: the returned ints is not a Variable (just a plain LongTensor). autograd of the real valued indices passes
        through the values alone, not the integer indices used to instantiate the sparse matrix.

        :param ind: A Variable containing a matrix of N by K, where K is the number of indices.
        :param val: A Variable containing a vector of length N containing the values corresponding to the given indices
        :return: a triple (ints, props, vals). ints is an N*2^K by K matrix representing the N*2^K integer index-tuples that can
            be made by flooring or ceiling the indices in 'ind'. 'props' is a vector of length N*2^K, which indicates how
            much of the original value each integer index-tuple receives (based on the distance to the real-valued
            index-tuple). vals is vector of length N*2^K, containing the value of the corresponding real-valued index-tuple
            (ie. vals just repeats each value in the input 'val' 2^K times).
        """

        batchsize, n, rank = means.size()

        # ints is the same size as ind, but for every index-tuple in ind, we add an extra axis containing the 2^rank
        # integerized index-tuples we can make from that one real-valued index-tuple
        # ints = torch.cuda.FloatTensor(batchsize, n, 2 ** rank + additional, rank) if use_cuda else FloatTensor(batchsize, n, 2 ** rank, rank)
        t0 = time.time()

        if BATCH_NEIGHBORS:
            fm = self.floor_mask.unsqueeze(0).unsqueeze(0).expand(batchsize, n, 2 ** rank, rank)

            neighbor_ints = means.data.unsqueeze(2).expand(batchsize, n, 2 ** rank, rank).contiguous()


            neighbor_ints[fm] = neighbor_ints[fm].floor()
            neighbor_ints[~fm] = neighbor_ints[~fm].ceil()

            neighbor_ints = neighbor_ints.long()

        else:
            neighbor_ints = LongTensor(batchsize, n, 2 ** rank, rank)

            # produce all integer index-tuples that neighbor the means
            for row in range(n):
                for t, bools in enumerate(itertools.product([True, False], repeat=rank)):

                    for col, bool in enumerate(bools):
                        r = means[:, row, col].data
                        neighbor_ints[:, row, t, col] = torch.floor(r) if bool else torch.ceil(r)

        # Sample additional points
        if rng is not None:
            t0 = time.time()
            total = util.prod(rng)

            if PROPER_SAMPLING:

                ints_flat = LongTensor(batchsize, n, 2 ** rank + additional)

                # flatten
                if neighbor_ints.is_cuda:
                    neighbor_ints = neighbor_ints.cpu()
                neighbor_ints = fi(neighbor_ints.view(-1, rank), rng, use_cuda=False)
                neighbor_ints = neighbor_ints.unsqueeze(0).view(batchsize, n, 2 ** rank)

                for b in range(batchsize):
                    for m in range(n):
                        sample = util.sample(range(total), additional + 2 ** rank, list(neighbor_ints[b, m, :]))
                        ints_flat[b, m, :] = LongTensor(sample)

                ints = tup(ints_flat.view(-1), rng, use_cuda=False)
                ints = ints.unsqueeze(0).unsqueeze(0).view(batchsize, n, 2 ** rank + additional, rank)
                ints_fl = ints.float().cuda() if use_cuda else ints.float()

            else:
                """
                Sample uniformly from all possible index-tuples, with replacement
                """
                sampled_ints = torch.cuda.FloatTensor(batchsize, n, additional, rank) if use_cuda else FloatTensor(batchsize, n, additional, rank)

                sampled_ints.uniform_()
                sampled_ints *= (1.0 - EPSILON)

                rng = torch.cuda.FloatTensor(rng) if use_cuda else FloatTensor(rng)
                rngxp = rng.unsqueeze(0).unsqueeze(0).unsqueeze(0).expand_as(sampled_ints)

                sampled_ints = torch.floor(sampled_ints * rngxp).long()


                if relative_range is not None:
                    """
                    Sample uniformly from a small range around the given index tuple
                    """
                    rr_ints = torch.cuda.FloatTensor(batchsize, n, self.rr_additional, rank) if use_cuda else FloatTensor(batchsize, n, self.rr_additional, rank)

                    rr_ints.uniform_()
                    rr_ints *= (1.0 - EPSILON)

                    rngxp = rng.unsqueeze(0).unsqueeze(0).unsqueeze(0).expand_as(rr_ints) # bounds of the tensor
                    rrng = torch.cuda.FloatTensor(relative_range) if use_cuda else FloatTensor(relative_range) # bounds of the range from which to sample
                    rrng = rrng.unsqueeze(0).unsqueeze(0).unsqueeze(0).expand_as(rr_ints)

                    mns_expand = means.round().unsqueeze(2).expand_as(rr_ints)

                    # upper and lower bounds
                    lower = mns_expand - rrng * 0.5
                    upper = mns_expand + rrng * 0.5

                    # check for any ranges that are out of bounds
                    idxs = lower < 0.0
                    lower[idxs] = 0.0

                    idxs = upper > rngxp
                    lower[idxs] = rngxp[idxs] - rrng[idxs]

                    # print('means', means.round().long())
                    # print('lower', lower)

                    rr_ints = (rr_ints * rrng + lower).long()

                samples = [neighbor_ints, sampled_ints, rr_ints] if relative_range is not None else [neighbor_ints, sampled_ints]
                ints = torch.cat(samples, dim=2)
                ints_fl = ints.float()

        ints_fl = Variable(ints_fl)  # leaf node in the comp graph, gradients go through values

        t0 = time.time()
        # compute the proportion of the value each integer index tuple receives
        props = densities(ints_fl, means, sigmas)
        # props is batchsize x K x 2^rank+a, giving a weight to each neighboring or sampled integer-index-tuple

        # -- normalize the proportions of the neigh points and the
        sums = torch.sum(props + EPSILON, dim=2, keepdim=True).expand_as(props)
        props = props / sums

        t0 = time.time()

        # repeat each value 2^rank+A times, so it matches the new indices
        val = torch.unsqueeze(values, 2).expand_as(props).contiguous()

        # 'Unroll' the ints tensor into a long list of integer index tuples (ie. a matrix of n*2^rank by rank for each
        # instance in the batch) ...
        ints = ints.view(batchsize, -1, rank, 1).squeeze(3)

        # ... and reshape the props and vals the same way
        props = props.view(batchsize, -1)
        val = val.view(batchsize, -1)

        return ints, props, val

    def forward(self, input, train=True):

        ### Compute and unpack output of hypernetwork

        t0 = time.time()
        bias = None

        if self.bias_type == Bias.NONE:
            means, sigmas, values = self.hyper(input)
        elif self.bias_type == Bias.DENSE:
            means, sigmas, values, bias = self.hyper(input)
        elif self.bias_type == Bias.SPARSE:
            means, sigmas, values, bias_means, bias_sigmas, bias_values = self.hyper(input)
        else:
            raise Exception('bias type {} not recognized.'.format(self.bias_type))


        if self.sparse_input:
            input = input.dense()

        return self.forward_inner(input, means, sigmas, values, bias, train=train)

    def forward_inner(self, input, means, sigmas, values, bias, train=True):

        t0total = time.time()

        rng = tuple(self.out_size) + tuple(input.size()[1:])

        batchsize = input.size()[0]

        # NB: due to batching, real_indices has shape batchsize x K x rank(W)
        #     real_values has shape batchsize x K

        # print('--------------------------------')
        # for i in range(util.prod(sigmas.size())):
        #     print(sigmas.view(-1)[i].data[0])

        # turn the real values into integers in a differentiable way
        t0 = time.time()

        if train:
            if not self.reinforce:
                if self.subsample is None:
                    indices, props, values = self.discretize(means, sigmas, values, rng=rng, additional=self.additional,
                    use_cuda=self.use_cuda, relative_range=self.relative_range)

                    values = values * props
                else: # select a small proportion of the indices to learn over

                    b, k, r = means.size()

                    prop = torch.cuda.FloatTensor([self.subsample]) if self.use_cuda else torch.FloatTensor([self.subsample])

                    selection = None
                    while (selection is None) or (float(selection.sum()) < 1):
                        selection = torch.bernoulli(prop.expand(k)).byte()

                    mselection = selection.unsqueeze(0).unsqueeze(2).expand_as(means)
                    sselection = selection.unsqueeze(0).unsqueeze(2).expand_as(sigmas)
                    vselection = selection.unsqueeze(0).expand_as(values)

                    means_in, means_out = means[mselection].view(b, -1, r), means[~ mselection].view(b, -1, r)
                    sigmas_in, sigmas_out = sigmas[sselection].view(b, -1, r), sigmas[~ sselection].view(b, -1, r)
                    values_in, values_out = values[vselection].view(b, -1), values[~ vselection].view(b, -1)

                    means_out = means_out.detach()
                    values_out = values_out.detach()

                    indices_in, props, values_in = self.discretize(means_in, sigmas_in, values_in, rng=rng, additional=self.additional, use_cuda=self.use_cuda)
                    values_in = values_in * props

                    indices_out = means_out.data.round().long()

                    indices = torch.cat([indices_in, indices_out], dim=1)
                    values = torch.cat([values_in, values_out], dim=1)

            else: # reinforce approach
                dists = torch.distributions.Normal(means, sigmas)
                samples = dists.sample()

                indices = samples.data.round().long()

                # if the sampling puts the indices out of bounds, we just clip to the min and max values
                indices[indices < 0] = 0

                rngt = torch.tensor(data=rng, device='cuda' if self.use_cuda else 'cpu')

                maxes = rngt.unsqueeze(0).unsqueeze(0).expand_as(means) - 1
                indices[indices > maxes] = maxes[indices > maxes]

        else: # not train, just use the nearest indices
            indices = means.round().long()

        if self.use_cuda:
            indices = indices.cuda()

        # # Create bias for permutation matrices
        # TAU = 1
        # if SINKHORN_ITS is not None:
        #     values = values / TAU
        #     for _ in range(SINKHORN_ITS):
        #         values = util.normalize(indices, values, rng, row=True)
        #         values = util.normalize(indices, values, rng, row=False)

        # translate tensor indices to matrix indices
        t0 = time.time()

        # mindices, flat_size = flatten_indices(indices, input.size()[1:], self.out_shape, self.use_cuda)
        mindices, flat_size = flatten_indices_mat(indices, input.size()[1:], self.out_size)

        # NB: mindices is not an autograd Variable. The error-signal for the indices passes to the hypernetwork
        #     through 'values', which are a function of both the real_indices and the real_values.

        ### Create the sparse weight tensor

        # -- Turns out we don't have autograd over sparse tensors yet (let alone over the constructor arguments). For
        #    now, we'll do a slow, naive multiplication.

        x_flat = input.view(batchsize, -1)
        ly = prod(self.out_size)

        y_flat = torch.cuda.FloatTensor(batchsize, ly) if self.use_cuda else FloatTensor(batchsize, ly)
        y_flat.fill_(0.0)

        sparsemult = util.sparsemult(self.use_cuda)

        t0 = time.time()

        # Prevent segfault
        assert not util.contains_nan(values.data)

        bm = self.bmult(flat_size[1], flat_size[0], mindices.size()[1], batchsize, self.use_cuda)
        bfsize = Variable(flat_size * batchsize)

        bfindices = mindices + bm
        bfindices = bfindices.view(1, -1, 2).squeeze(0)
        vindices = Variable(bfindices.t())

        bfvalues = values.view(1, -1).squeeze(0)
        bfx = x_flat.view(1, -1).squeeze(0)

        # print(vindices.size(), bfvalues.size(), bfsize, bfx.size())
        bfy = sparsemult(vindices, bfvalues, bfsize, bfx)

        y_flat = bfy.unsqueeze(0).view(batchsize, -1)

        y_shape = [batchsize]
        y_shape.extend(self.out_size)

        y = y_flat.view(y_shape) # reshape y into a tensor

        ### Handle the bias
        if self.bias_type == Bias.DENSE:
            y = y + bias
        if self.bias_type == Bias.SPARSE: # untested!
            pass

        if self.reinforce and train:
            return y, dists, samples
        else:
            return y

    def forward_sample(self, input):
        """
        Samples a single sparse matrix, and computes a transformation with that in a non-differentiable manner.

        :param input:
        :return:
        """

        # Sample k indices



    def backward_sample(self, batch_loss, q_prob, p_prob):
        """
        Computes the gradient by REINFORCE, using the given batch loss, and the probabilities of the sample (as returned by forward_sample)
        :param bacth_loss:
        :param q_prob:
        :param p_prob:
        :return:
        """

class ParamASHLayer(HyperLayer):
    """
    Hyperlayer with free sparse parameters, no hypernetwork (not stricly ASH, should rename).
    """

    def __init__(self, in_shape, out_shape, k, additional=0, sigma_scale=0.2, fix_values=False,  has_bias=False,
                 subsample=None, min_sigma=0.0, reinforce=False, relative_range=None, rr_additional=None):
        super().__init__(in_rank=len(in_shape), additional=additional, out_shape=out_shape,
                         bias_type=Bias.DENSE if has_bias else Bias.NONE, subsample=subsample,
                         reinforce=reinforce, relative_range=relative_range,
                         rr_additional=rr_additional)

        self.k = k
        self.in_shape = in_shape
        self.out_shape = out_shape
        self.sigma_scale = sigma_scale
        self.fix_values = fix_values
        self.has_bias = has_bias
        self.min_sigma = min_sigma

        self.w_rank = len(in_shape) + len(out_shape)

        p = torch.randn(k, self.w_rank + 2)

        # p[:, self.w_rank:self.w_rank + 1] = p[:, self.w_rank:self.w_rank + 1]

        self.params = Parameter(p)

        if self.has_bias:
            self.bias = Parameter(torch.randn(*out_shape))

    def hyper(self, input):
        """
        Evaluates hypernetwork.
        """

        batch_size = input.size()[0]

        # Replicate the parameters along the batch dimension
        res = self.params.unsqueeze(0).expand(batch_size, self.k, self.w_rank+2)

        means, sigmas, values = self.split_out(res, input.size()[1:], self.out_shape)
        sigmas = sigmas * self.sigma_scale + self.min_sigma

        if self.fix_values:
            values = values * 0.0 + 1.0

        if self.has_bias:
            return means, sigmas, values, self.bias

        return means, sigmas, values

    def clone(self):
        result = ParamASHLayer(self.in_shape, self.out_shape, self.k, self.additional, self.gain)

        result.params = Parameter(self.params.data.clone())

        return result

class WeightSharingASHLayer(HyperLayer):
    """
    Hyperlayer with free sparse parameters, no hypernetwork, and a limited number of weights with hard sharing
    """

    def __init__(self, in_shape, out_shape, k, additional=0, sigma_scale=0.1, num_values=2, subsample=None):
        super().__init__(in_rank=len(in_shape), additional=additional, out_shape=out_shape, bias_type=Bias.NONE, subsample=subsample)

        self.k = k
        self.in_shape = in_shape
        self.out_shape = out_shape
        self.sigma_scale = sigma_scale

        self.w_rank = len(in_shape) + len(out_shape)

        p = torch.randn(k, self.w_rank + 1 + num_values)
        p[:, self.w_rank:self.w_rank + 1] = p[:, self.w_rank:self.w_rank + 1]
        self.params = Parameter(p)

        self.sources = Parameter(torch.randn(num_values))
        # self.sources = Variable(FloatTensor([-1.0, 1.0]))

    def hyper(self, input):
        """
        Evaluates hypernetwork.
        """

        batch_size = input.size()[0]

        # Replicate the parameters along the batch dimension
        rows, columns = self.params.size()
        res = self.params.unsqueeze(0).expand(batch_size, rows, columns)

        means, sigmas, values, self.samples = self.split_shared(res, input.size()[1:], self.out_shape, self.sources)
        sigmas = sigmas * self.sigma_scale

        return means, sigmas, values

    def call_reinforce(self, downstream_reward):
        b, = downstream_reward.size()

        rew = downstream_reward.unsqueeze(1).expand(b, self.k)
        rew = rew.contiguous().view(-1, 1)

        self.samples.reinforce(rew)
        self.samples.backward()

    def clone(self):

        result = ParamASHLayer(self.in_shape, self.out_shape, self.k, self.additional, self.gain)

        result.params = Parameter(self.params.data.clone())

        return result

class ImageCASHLayer(HyperLayer):
    """
    """

    def __init__(self, in_shape, out_shape, k, additional=0, poolsize=4, subsample=None):
        super().__init__(in_rank=len(in_shape), out_shape=out_shape, additional=additional, bias_type=Bias.DENSE, subsample=subsample)

        self.k = k
        self.in_shape = in_shape
        self.out_shape = out_shape

        rep = 4*4*4*2

        self.w_rank = len(in_shape) + len(out_shape)

        c, x, y = in_shape
        flat_size = int(x/poolsize) * int(y/poolsize) * c

        # hypernetwork
        self.tohidden = nn.Sequential(
            nn.MaxPool2d(kernel_size=poolsize, stride=poolsize),
            Flatten(),
            nn.Linear(flat_size, int(k/rep)),
            nn.ReLU()
        )

        self.conv1da = nn.ConvTranspose1d(in_channels=1, out_channels=1, kernel_size=4, stride=4)
        self.conv1db = nn.ConvTranspose1d(in_channels=1, out_channels=1, kernel_size=4, stride=4)
        self.conv1dc = nn.ConvTranspose1d(in_channels=1, out_channels=1, kernel_size=4, stride=4)

        self.conv2d = nn.ConvTranspose2d(in_channels=1, out_channels=1, kernel_size=(2, self.w_rank+2), stride=2)

        self.bias = nn.Sequential(
            nn.Linear(int(k/rep), util.prod(out_shape)),
        )

    def hyper(self, input):
        """
        Evaluates hypernetwork.
        """

        insize = input.size()

        hidden = self.tohidden(input)

        res = hidden

        res = res.unsqueeze(1)
        res = nn.functional.relu(self.conv1da(res))
        res = res.squeeze(1)

        res = res.unsqueeze(1)
        res = nn.functional.relu(self.conv1db(res))
        res = res.squeeze(1)

        res = res.unsqueeze(1)
        res = nn.functional.relu(self.conv1dc(res))
        res = res.squeeze(1)

        res = res.unsqueeze(1).unsqueeze(3)
        res = nn.functional.relu(self.conv2d(res))
        res = res.squeeze(1)

        means, sigmas, values = self.split_out(res, input.size()[1:], self.out_shape)

        bias = self.bias(hidden)
        bias = bias.view((-1, ) + self.out_shape)

        return means, sigmas, values, bias

class CASHLayer(HyperLayer):
    """

    """
    def __init__(self, in_shape, out_shape, k,
                 additional=0, poolsize=4, deconvs=2, ksize=2, sigma_scale=0.1, has_bias=True,
                 has_channels=False, adaptive_bias=False, subsample=None, min_sigma=0.0, fix_values=False):
        """
        :param in_shape:
        :param out_shape:
        :param k: How many index tuples to generate. If this is not divisible by 2^deconvs, you'll get the next biggest
        number that is.
        :param poolsize:
        :param has_channels: If true, the first non-batch dimension is interpreted as a 'channel dimension', which means
           that the input is not downsampled along that dimension.
        :param deconvs: How many deconv layers to use to generate the tuples from the hidden layer
        """
        super().__init__(in_rank=len(in_shape), out_shape=out_shape, additional=additional, bias_type=Bias.DENSE if has_bias else Bias.NONE, subsample=subsample)

        class NoActivation(nn.Module):
            def forward(self, input):
                return input

        self.activation = NoActivation()

        self.has_bias = has_bias
        self.k = k
        self.in_shape = in_shape
        self.out_shape = out_shape
        self.sigma_scale = sigma_scale
        self.has_channels = has_channels
        self.adaptive_bias = adaptive_bias
        self.fix_values = fix_values
        self.min_sigma = min_sigma

        self.w_rank = len(in_shape) + len(out_shape)

        self.ha = int(math.ceil(k/ksize**deconvs))
        self.hb = 8

        print('hidden size is  ', self.ha,' by ', self.hb)

        c_in_shape = in_shape[1:] if has_channels else in_shape

        if len(c_in_shape) == 1:
            x = c_in_shape[0]
            flat_size = int(x / poolsize)
            self.pool = nn.AvgPool1d(kernel_size=poolsize, stride=poolsize)
        elif len(c_in_shape) == 2:
            x, y = c_in_shape
            flat_size = int(x / poolsize) * int(y / poolsize)
            self.pool = nn.AvgPool2d(kernel_size=poolsize, stride=poolsize)
        elif len(c_in_shape) == 3:
            x, y, z = c_in_shape
            flat_size = int(x / poolsize) * int(y / poolsize) * int(z / poolsize)
            self.pool = nn.AvgPool3d(kernel_size=poolsize, stride=poolsize)
        else:
            raise Exception('Input dimensions higher than 3 not supported (yet)')

        if self.has_channels:
            flat_size *= in_shape[0]

        # hypernetwork
        self.tohidden = nn.Sequential(
            Flatten(),
            nn.Linear(flat_size, self.ha * self.hb),
            self.activation
        )

        self.conv1 = nn.ConvTranspose1d(in_channels=self.hb, out_channels=self.w_rank+2, kernel_size=ksize, stride=ksize)

        self.convs = nn.ModuleList()
        for i in range(deconvs - 1):
            self.convs.append(
                nn.ConvTranspose1d(in_channels=self.w_rank+2, out_channels=self.w_rank+2, kernel_size=ksize, stride=ksize))

        if self.adaptive_bias:
            self.bias = nn.Sequential(
                nn.Linear(self.ha * self.hb, util.prod(out_shape)),
                self.activation
            )
        else:
            self.bias = Parameter(torch.zeros(*out_shape))

    def hyper(self, input):
        """
        Evaluates hypernetwork.
        """

        insize = input.size()

        if self.has_channels:
            downsampled = self.pool(input)
        else:
            downsampled = self.pool(input.unsqueeze(1)).squeeze(1)

        hidden = self.tohidden(downsampled)

        res = hidden.view(insize[0], self.hb, self.ha)

        res = self.conv1(res)

        for i, conv in enumerate(self.convs):
            if i != 0:
                res = self.activation(res)
            res = conv(res)

        res = res.transpose(1,2)
        # res has shape batch_size x k x rank+2

        means, sigmas, values = self.split_out(res, input.size()[1:], self.out_shape)
        sigmas = sigmas * self.sigma_scale + self.min_sigma

        if self.fix_values:
            values = values * 0.0 + 1.0

        if not self.has_bias:
            return means, sigmas, values
        if self.adaptive_bias:
            bias = self.bias(hidden)
        else:
            bias = self.bias

        return means, sigmas, values, bias

class WSCASHLayer(HyperLayer):
    """
    Weight-sharing (de)convolutional ASH layer
    """
    def __init__(self, in_shape, out_shape, k, additional=0, poolsize=4, deconvs=2, sigma_scale=0.1, num_sources=2, has_bias=True, subsample=None):
        """
        :param in_shape:
        :param out_shape:
        :param k: How many index tuples to generate. If this is not divisible by 2^deconvs, you'll get the next biggest
        number that is.
        :param poolsize:
        :param deconvs: How many deconv layers to use to generate the tuples from the hidden layer
        """
        super().__init__(in_rank=len(in_shape), out_shape=out_shape, additional=additional, bias_type=Bias.DENSE if has_bias else Bias.NONE, subsample=subsample)

        class NoActivation(nn.Module):
            def forward(self, input):
                return input

        self.activation = NoActivation()

        self.has_bias = has_bias
        self.k = k
        self.in_shape = in_shape
        self.out_shape = out_shape
        self.sigma_scale = sigma_scale

        self.w_rank = len(in_shape) + len(out_shape)

        width = self.w_rank + 1 + num_sources # nr of parameters per index-tuple

        self.ha = int(math.ceil(k/2**deconvs))
        self.hb = 8

        if len(in_shape) == 1:
            x = in_shape[0]
            flat_size = int(x / poolsize)
            self.pool = nn.AvgPool1d(kernel_size=poolsize, stride=poolsize)
        elif len(in_shape) == 2:
            x, y = in_shape
            flat_size = int(x / poolsize) * int(y / poolsize)
            self.pool = nn.AvgPool2d(kernel_size=poolsize, stride=poolsize)
        elif len(in_shape) == 3:
            x, y, z = in_shape
            flat_size = int(x / poolsize) * int(y / poolsize) * int(z / poolsize)
            self.pool = nn.AvgPool3d(kernel_size=poolsize, stride=poolsize)
        else:
            raise Exception('Input dimensions higher than 3 not supported (yet)')

        # hypernetwork
        self.tohidden = nn.Sequential(
            Flatten(),
            nn.Linear(flat_size, self.ha * self.hb),
            self.activation
        )

        self.conv1= nn.ConvTranspose1d(in_channels=self.hb, out_channels=width, kernel_size=2, stride=2)

        self.convs = nn.ModuleList()
        for i in range(deconvs - 1):
            self.convs.append(
                nn.ConvTranspose1d(in_channels=width, out_channels=width, kernel_size=2, stride=2))

        self.bias = nn.Sequential(
            nn.Linear(self.ha * self.hb, util.prod(out_shape)),
            self.activation
        )

        self.sources = Parameter(torch.randn(num_sources))
        # self.sources = Variable(FloatTensor([-1.0, 1.0]))

    def hyper(self, input):
        """
        Evaluates hypernetwork.
        """

        insize = input.size()

        downsampled = self.pool(input.unsqueeze(1)).unsqueeze(1)

        hidden = self.tohidden(downsampled)

        res = hidden.view(insize[0], self.hb, self.ha)

        res = self.conv1(res)

        for i, conv in enumerate(self.convs):
            if i != 0:
                res = self.activation(res)
            res = conv(res)

        res = res.transpose(1,2)

        means, sigmas, values, self.samples = self.split_shared(res, input.size()[1:], self.out_shape, self.sources)
        sigmas = sigmas * self.sigma_scale

        if not self.has_bias:
            return means, sigmas, values

        bias = self.bias(hidden)
        return means, sigmas, values, bias

    def call_reinforce(self, downstream_reward):
        b, = downstream_reward.size()

        rew = downstream_reward.unsqueeze(1).expand(b, self.k)
        rew = rew.contiguous().view(-1, 1)

        self.samples.reinforce(rew)
        self.samples.backward(retain_graph=True)

    # ints = LongTensor(range(hyper.prod(SHAPE)))
    # print(ints)
    # ints = ints.unsqueeze(0).unsqueeze(0)
    # ints = ints.view(SHAPE)
    # print(ints)
    #
    # i = LongTensor(range(hyper.prod(SHAPE)))
    # t = tup(i, SHAPE)
    # for row in range(t.size()[0]):
    #     tup = tuple(t[row, :].squeeze(0))
    #     print(ints[tup])
