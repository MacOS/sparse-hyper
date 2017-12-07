import hyper, gaussian, util, logging, time, pretrain
import torch, random
from torch.autograd import Variable
from torch import nn, optim
from tqdm import trange, tqdm
from tensorboardX import SummaryWriter
from util import Lambda, Debug

from torchsample.metrics import CategoricalAccuracy

import torch.optim as optim

import torchvision
import torchvision.transforms as transforms

from util import od

torch.manual_seed(1)
logging.basicConfig(filename='run.log',level=logging.INFO)
LOG = logging.getLogger()

"""
MNIST experiment

"""
w = SummaryWriter()

BATCH = 256
SHAPE = (28, 28)
EPOCHS = 350
PRETRAIN = True

CUDA = False

gaussian.PROPER_SAMPLING = False

TYPE = 'baseline'

normalize = transforms.Compose(
    [transforms.ToTensor()])

train = torchvision.datasets.MNIST(root='./data', train=True,
                                        download=True, transform=normalize)

trainloader = torch.utils.data.DataLoader(train, batch_size=BATCH,
                                          shuffle=True, num_workers=2)

test = torchvision.datasets.MNIST(root='./data', train=False,
                                       download=True, transform=normalize)

testloader = torch.utils.data.DataLoader(test, batch_size=BATCH,
                                         shuffle=False, num_workers=2)

if TYPE == 'non-adaptive':
    layers = [
        gaussian.ParamASHLayer(SHAPE, (4, 8, 8), k=64, additional=8, has_bias=True),
        nn.Sigmoid(),
        gaussian.ParamASHLayer((4, 8, 8), (128,), k=64, additional=32, has_bias=True),
        nn.Sigmoid(),
        nn.Linear(128, 10),
        nn.Softmax()]

elif TYPE == 'free':

    shapes = [(28, 28), (4, 16, 16), (8, 4, 4), (128,)]
    layers = [
        gaussian.CASHLayer(shapes[0], shapes[1], k=1500, additional=256, has_bias=True, has_channels=False),
        nn.Sigmoid(),
        gaussian.CASHLayer(shapes[1], shapes[2], k=750, additional=128, has_bias=True, has_channels=True),
        nn.Sigmoid(),
        gaussian.CASHLayer(shapes[2], shapes[3], k=750, additional=128, has_bias=True, has_channels=True),
        nn.Sigmoid(),
        nn.Linear(shapes[3][0], 10),
        nn.Softmax()]
    pivots = [2, 4, 6]
    decoder_channels = [True, True, False]

    if PRETRAIN:
        pretrain.pretrain(layers, shapes, pivots, trainloader, epochs=5, k_out=256, out_additional=128, use_cuda=CUDA,
                          plot=True, has_channels=decoder_channels)

    model = nn.Sequential(od(layers))

elif TYPE == 'baseline':
    model = nn.Sequential(
        Lambda(lambda x : x.unsqueeze(1)),
        # Debug(lambda x: print('0', x.size(), util.prod(x[-1:].size()))),
        nn.Conv2d(in_channels=1, out_channels=4, kernel_size=5, stride=1, padding=4),
        nn.MaxPool2d(stride=2, kernel_size=2),
        # Debug(lambda x: print('1', x.size(), util.prod(x[-1:].size()))),
        nn.Conv2d(in_channels=4, out_channels=8, kernel_size=5, stride=1, padding=2),
        nn.MaxPool2d(stride=2, kernel_size=2),
        # Debug(lambda x: print('2', x.size(), util.prod(x[-1:].size()))),
        util.Flatten(),
        # Debug(lambda x: print('3', x.size(), util.prod(x[-1:].size()))),
        nn.Linear(512, 10),
        nn.Softmax())

if CUDA:
    model.apply(lambda t : t.cuda())

## SIMPLE
criterion = nn.CrossEntropyLoss()
acc = CategoricalAccuracy()
optimizer = optim.Adam(model.parameters(), lr=0.01)

step = 0

for epoch in range(EPOCHS):
    for i, data in tqdm(enumerate(trainloader, 0)):

        # get the inputs
        inputs, labels = data
        inputs = inputs.squeeze(1)

        if CUDA:
            inputs, labels = inputs.cuda(), labels.cuda()

        # wrap them in Variables
        inputs, labels = Variable(inputs), Variable(labels)

        # forward + backward + optimize
        optimizer.zero_grad()
        outputs = model(inputs)
        loss = criterion(outputs, labels)

        t0 = time.time()
        loss.backward()  # compute the gradients
        logging.info('backward: {} seconds'.format(time.time() - t0))
        optimizer.step()

        w.add_scalar('mnist/train-loss', loss.data[0], step)

        step += 1

    total = 0.0
    num = 0
    for i, data in enumerate(testloader, 0):
        # get the inputs
        inputs, labels = data
        inputs = inputs.squeeze(1)

        if CUDA:
            inputs, labels = inputs.cuda(), labels.cuda()

        # wrap them in Variables
        inputs, labels = Variable(inputs), Variable(labels)

        # forward + backward + optimize
        optimizer.zero_grad()
        outputs = model(inputs)

        total += acc(outputs, labels)
        num += 1

    accuracy = total/num

    w.add_scalar('mnist/per-epoch-test-acc', accuracy, epoch)
    print('EPOCH {}: {} accuracy '.format(epoch, accuracy))

print('Finished Training.')

