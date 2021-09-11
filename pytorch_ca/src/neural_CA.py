import torch
from torch import nn
import torch.nn.functional as F
import torchvision.transforms as T
import os.path
from random import randint
from IPython.display import clear_output
from typing import Callable, Tuple
from abc import abstractmethod
import numpy as np

from .utils import *
from .sample_pool import *


class CAModel(nn.Module):
    """Base CA class, each CA class inherits from this class
    """

    def __init__(self):
        super(CAModel, self).__init__()

    @abstractmethod
    def forward():
        pass

    def evolve(self, x: torch.Tensor, iters: int, angle: float = 0.,
               step_size: float = 1.) -> torch.Tensor:
        """Evolves the input images "x" for "iters" steps

        Args:
            x (torch.Tensor): Previous CA state
            iters (int): Number of steps to perform
            angle (float, optional): Angle of the update. Defaults to 0..
            step_size (float, optional): Step size of the update. Defaults to 1..

        Returns:
            torch.Tensor: dx
        """
        self.eval()
        with torch.no_grad():
            for i in range(iters):
                x = self.forward(x, angle=angle, step_size=step_size)

        return x

    def test_CA(self,
                criterion: Callable[[torch.Tensor], torch.Tensor],
                images: torch.Tensor,
                evolution_iters: int = 1000,
                batch_size: int = 32) -> torch.Tensor:
        """Evaluates the model over the given images by evolving them
            and computing the loss against the target at each iteration.
            Returns the mean loss at each iteration

        Args:
            criterion (Callable[[torch.Tensor], torch.Tensor]): Loss function
            images (torch.Tensor): Images to evolve
            evolution_iters (int, optional): Evolution steps. Defaults to 1000.
            batch_size (int, optional): Batch size. Defaults to 32.

        Returns:
            torch.Tensor: tensor of size (evolution_iters) 
                which contains the mean loss at each iteration
        """

        self.eval()
        evolution_losses = torch.zeros((evolution_iters), device="cpu")
        eval_samples = images.size()[0]

        n = 0
        with torch.no_grad():
            for i in range(eval_samples // batch_size):
                for j in range(evolution_iters):
                    inputs = self.forward(inputs)
                    loss, _ = criterion(inputs)

                    # Updates the average error
                    evolution_losses[j] = (n*evolution_losses[j] +
                                           batch_size*loss.cpu()) / (n+batch_size)

                n += batch_size

        return evolution_losses


class TrainCA():
    def train_CA(self,
                 optimizer: torch.optim.Optimizer,
                 criterion: Callable[[torch.Tensor], Tuple[torch.Tensor, torch.Tensor]],
                 pool: SamplePool,
                 n_epochs: int,
                 scheduler: torch.optim.lr_scheduler._LRScheduler = None,
                 batch_size: int = 4,
                 skip_update: int = 2,
                 evolution_iters: Tuple[int, int] = (50, 60),
                 kind: str = "growing",
                 n_max_losses: int = 1,
                 **kwargs):
        """Trains the CA model

        Args:
            optimizer (torch.optim.Optimizer): Optimizer to use, recommended Adam

            criterion (Callable[[torch.Tensor], Tuple[torch.Tensor,torch.Tensor]]): Loss function to use

            pool (SamplePool): Sample pool from which to extract the images

            n_epochs (int): Number of epochs to perform, _
                this depends on the size of the sample pool

            scheduler (torch.optim.lr_scheduler._LRScheduler, optional):
                 Learning rate scheduler. Defaults to None.

            batch_size (int, optional): Batch size. Defaults to 4.

            skip_update (int, optional): How many batches to process before
                the image with maximum loss is replaced with a new seed.
                Defaults to 2, i.e. substitute the image with maximum loss
                every 2 iterations.

            evolution_iters (Tuple[int, int], optional):
                Minimum and maximum number of evolution iterations to perform.
                Defaults to (50, 60).

            kind (str, optional): 
                Kind of CA to train, can be either one of:
                    growing: Trains a CA that grows into the target image
                    persistent: Trains a CA that grows into the target image
                        and persists
                    regenerating: Trains a CA that grows into the target image
                                  and regenerates any damage that it receives
                Defaults to "growing".
            n_max_losses(int):
                number of datapoints with the biggest losses to replace.
                Defaults to 1
        """

        self.train()

        for i in range(n_epochs):
            epoch_losses = []  # array that stores the loss history

            # take the data
            for j in range(pool.size // batch_size):
                inputs, indexes = pool.sample(batch_size)  # sample the inputs
                # put them in the current device
                inputs = inputs.to(self.device)
                optimizer.zero_grad()  # reinitialize the gradient to zero

                # recursive forward-pass
                for k in range(randint(*evolution_iters)):
                    inputs = self.forward(inputs)
                    
                # calculate the loss of the inputs and return the ones with the biggest loss
                loss, idx_max_loss = criterion(inputs, n_max_losses)
                # add current loss to the loss history
                epoch_losses.append(loss.item())

                # look a definition of skip_update
                if j % skip_update != 0:
                    idx_max_loss = None

                # backward-pass
                loss.backward()
                optimizer.step()

                # customization of training for the three processes of growing. persisting and regenerating

                # if regenerating, then damage inputs
                if kind == "regenerating":
                    inputs = inputs.detach()
                    # damages the inputs by removing square portions
                    inputs = make_squares(inputs, **kwargs)

                # if training is not for growing proccess then re-insert trained/damaged samples into the pool
                if kind != "growing":
                    idx_max_loss = [indexes[i] for i in idx_max_loss]
                    pool.update(indexes, inputs, idx_max_loss)

            # update the scheduler if there is one at all
            if scheduler is not None:
                scheduler.step()

            self.losses.append(np.mean(epoch_losses))
            print(f"epoch: {i+1}\navg loss: {np.mean(epoch_losses)}")
            clear_output(wait=True)


class NeuralCA(CAModel, TrainCA):
    """Implements a neural cellular automata model like described here
    https://distill.pub/2020/growing-ca/
    """

    def __init__(self, n_channels: int = 16,
                 device: torch.device = None,  # ma non è inutile questo argomento?
                 fire_rate: float = 0.5):
        """Initializes the network.

        Args:
            n_channels (int, optional): Number of input channels.
                Defaults to 16.
            device (torch.device, optional): Device where to store the net.
                Defaults to None.
            fire_rate (float, optional): Probability to reject an update.
                Defaults to 0.5.
        """

        super().__init__()

        if device is None:
            device = torch.device(
                "cuda" if torch.cuda.is_available() else "cpu")
        self.device = device

        self.n_channels = n_channels

        self.fire_rate = fire_rate

        # Stores losses during training
        self.losses = []

        # Network layers needed for the update rule
        self.layers = nn.Sequential(
            nn.Conv2d(n_channels*3, 128, 1),
            nn.ReLU(),
            nn.Conv2d(128, n_channels, 1))

        self.to(self.device)

    def perceive(self, images: torch.Tensor, angle: float = 0.) -> torch.Tensor:
        """Returns the perception vector of each cell in an image, or perception matrix

        Args:
            images (torch.Tensor): Images to compute the perception matrix
            angle (float, optional): Angle of the Sobel filters. Defaults to 0.

        Returns:
            torch.Tensor: Perception matrix
        """

        # Filters
        identity = torch.tensor([[0., 0., 0.],
                                 [0., 1., 0.],
                                 [0., 0., 0.]])
        dx = torch.tensor([[-0.125, 0., 0.125],
                           [-0.25, 0., 0.25],
                           [-0.125, 0., 0.125]])
        dy = dx.T

        # Rotation
        angle = torch.tensor(angle)
        c, s = torch.cos(angle), torch.sin(angle)
        dx, dy = c*dx - s*dy, s*dx + c*dy

        # Create filters batch
        all_filters = torch.stack((identity, dx, dy))
        all_filters_batch = all_filters.repeat(
            self.n_channels, 1, 1).unsqueeze(1)
        all_filters_batch = all_filters_batch.to(self.device)

        # Depthwise convolution over input images
        return F.conv2d(wrap_edges(images), all_filters_batch, groups=self.n_channels)

    def compute_dx(self, x: torch.Tensor, angle: float = 0.,
                   step_size: float = 1.) -> torch.Tensor:
        """Computes a single update dx

        Args:
            x (torch.Tensor): Previous CA state
            angle (float, optional): Angle of the update. Defaults to 0..
            step_size (float, optional): Step size of the update. Defaults to 1..

        Returns:
            torch.Tensor: dx
        """
        # compute update increment
        dx = self.layers(self.perceive(x, angle)) * step_size

        # get random-per-cell mask for stochastic update
        update_mask = torch.rand(
            x[:, :1, :, :].size(), device=self.device) < self.fire_rate

        return dx*update_mask.float()

    def forward(self, x: torch.Tensor,
                angle: float = 0.,
                step_size: float = 1.) -> torch.Tensor:
        """Single update step of the CA

        Args:
            x (torch.Tensor): Previous CA state
            angle (float, optional): Angle of the update. Defaults to 0.
            step_size (float, optional): Step size of the update. Defaults to 1.

        Returns:
            torch.Tensor: Next CA state
        """
        pre_life_mask = get_living_mask(x,4)

        x += self.compute_dx(x, angle, step_size)

        post_life_mask = get_living_mask(x,4)

        # get alive mask
        life_mask = pre_life_mask & post_life_mask

        # return updated states with alive masking
        return x * life_mask.float()

    def load(self, fname: str):
        """Loads a (pre-trained) model

        Args:
            fname (str): Path of the model to load
        """

        self.load_state_dict(torch.load(fname))
        print("Successfully loaded model!")

    def save(self, fname: str, overwrite: bool = False):
        """Saves a (trained) model

        Args:
            fname (str): Path where to save the model.
            overwrite (bool, optional): Whether to overwrite the existing file.
                Defaults to False..

        Raises:
            Exception: If the file already exists and
                the overwrite argument is set to False
        """
        if os.path.exists(fname) and not overwrite:
            message = "The file name already exists, to overwrite it set the "
            message += "overwrite argument to True to confirm the overwrite"
            raise Exception(message)
        torch.save(self.state_dict(), fname)
        print("Successfully saved model!")
