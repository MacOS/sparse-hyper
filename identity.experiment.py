import hyper, gaussian
import torch, random, sys
from torch.autograd import Variable
from torch.nn import Parameter
from torch import nn, optim
from tqdm import trange
from tensorboardX import SummaryWriter

import matplotlib.pyplot as plt
import util, logging, time, gc
import numpy as np

from argparse import ArgumentParser

import psutil, os

logging.basicConfig(filename='run.log',level=logging.INFO)
LOG = logging.getLogger()

"""
Simple experiment: learn the identity function from one tensor to another
"""
w = SummaryWriter()


def go(iterations=30000, additional=64, batch=4, size=32, cuda=False, plot_every=50, lr=0.01, fv=False, sigma_scale=0.1):

    SHAPE = (size,)
    MARGIN = 0.1

    torch.manual_seed(0)

    nzs = hyper.prod(SHAPE)

    plt.figure(figsize=(5,5))
    util.makedirs('./identity/')

    params = None

    gaussian.PROPER_SAMPLING = False
    model = gaussian.ParamASHLayer(SHAPE, SHAPE, k=size, additional=additional, sigma_scale=sigma_scale, has_bias=False, fix_values=fv)

    if cuda:
        model.cuda()

    criterion = nn.MSELoss()
    optimizer = optim.Adam(model.parameters(), lr=lr)


    for i in trange(iterations):

        x = torch.rand((batch,) + SHAPE)
        if cuda:
            x = x.cuda()
        x = Variable(x)

        optimizer.zero_grad()

        y = model(x)

        loss = criterion(y, x) # compute the loss

        t0 = time.time()
        loss.backward()        # compute the gradients

        optimizer.step()

        w.add_scalar('identity32/loss', loss.data[0], i*batch)

        if False or i % plot_every == 0:
            means, sigmas, values = model.hyper(x)

            plt.cla()
            util.plot(means, sigmas, values, shape=(SHAPE[0], SHAPE[0]))
            plt.xlim((-MARGIN*(SHAPE[0]-1), (SHAPE[0]-1) * (1.0+MARGIN)))
            plt.ylim((-MARGIN*(SHAPE[0]-1), (SHAPE[0]-1) * (1.0+MARGIN)))

            plt.savefig('./identity/means{:04}.png'.format(i))

if __name__ == "__main__":

    ## Parse the command line options
    parser = ArgumentParser()

    parser.add_argument("-b", "--batch-size",
                        dest="batch_size",
                        help="The batch size.",
                        default=64, type=int)

    parser.add_argument("-i", "--iterations",
                        dest="iterations",
                        help="The number of iterations (ie. the nr of batches).",
                        default=30000, type=int)

    parser.add_argument("-a", "--additional",
                        dest="additional",
                        help="Number of additional points sampled",
                        default=512, type=int)

    parser.add_argument("-c", "--cuda", dest="cuda",
                        help="Whether to use cuda.",
                        action="store_true")

    parser.add_argument("-F", "--fix_values", dest="fix_values",
                        help="Whether to fix the values to 1.",
                        action="store_true")

    parser.add_argument("-l", "--learn-rate",
                        dest="lr",
                        help="Learning rate",
                        default=0.01, type=float)

    parser.add_argument("-S", "--sigma-scale",
                        dest="sigma_scale",
                        help="Sigma scale",
                        default=0.1, type=float)

    parser.add_argument("-p", "--plot-every",
                        dest="plot_every",
                        help="Plot every x iterations",
                        default=50, type=int)

    options = parser.parse_args()

    print('OPTIONS ', options)
    LOG.info('OPTIONS ' + str(options))

    go(batch=options.batch_size,
        additional=options.additional, iterations=options.iterations, cuda=options.cuda,
        lr=options.lr, plot_every=options.plot_every, fv=options.fix_values, sigma_scale=options.sigma_scale)
