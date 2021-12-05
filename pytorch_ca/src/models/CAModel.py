import torch
from torch import nn

import pylab as pl
from random import randint
from IPython.display import clear_output
from typing import Callable, Tuple
from abc import abstractmethod
import numpy as np
import wandb

from ..utils import *
from ..sample_pool import *
from .. loss_functions import *


class CAModel(nn.Module):
    """Base CA class, each CA class inherits from this class
    """

    def __init__(self, n_channels=16, device=None, fire_rate=0.5):
        super(CAModel, self).__init__()

        # useless comment
        self.n_channels = n_channels
        self.alpha_channel = 3

        # defines the device
        if device is None:
            device = torch.device(
                "cuda" if torch.cuda.is_available() else "cpu")
        self.device = device

        # Stores losses during training
        self.losses = []

        self.fire_rate = fire_rate

        self.to(self.device)

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
            for i in range(0, eval_samples, batch_size):
                inputs = images[i:i+batch_size].to(self.device)
                for j in range(evolution_iters):
                    inputs = self.forward(inputs)
                    loss, _ = criterion(inputs,n_max_losses=0)

                    # Updates the average error
                    evolution_losses[j] = (n*evolution_losses[j] +
                                           batch_size*loss.cpu()) / (n+batch_size)

                n += batch_size

        return evolution_losses

    def train_CA(self,
                 optimizer: torch.optim.Optimizer,
                 criterion: Callable[[torch.Tensor], Tuple[torch.Tensor, torch.Tensor]],
                 pool: SamplePool,
                 n_epochs: int,
                 scheduler: torch.optim.lr_scheduler._LRScheduler = None,
                 batch_size: int = 4,
                 skip_update: int = 2,
                 evolution_iters: int = 96,
                 kind: str = "growing",
                 n_max_losses: int = 1,
                 normalize_gradients=False,
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
            n_max_losses(int,optional):
                number of datapoints with the biggest losses to replace.
                Defaults to 1
        """

        self.train()

        for epoch in range(n_epochs):
            epoch_losses = []  # array that stores the loss history

            # take the data
            for j in range(pool.size // batch_size):
                inputs, indexes = pool.sample(batch_size)  # sample the inputs
                # put them in the current device
                inputs = inputs.to(self.device)
                optimizer.zero_grad()  # reinitialize the gradient to zero

                self.update(inputs) #This is useful when you update the mask

                # recursive forward-pass
                total_loss=0
                for n_step in range(evolution_iters):
                    inputs = self.forward(inputs)
                    # calculate the loss of the inputs and return the ones with the biggest loss
                    losses = criterion(inputs, n_step, epoch)
                    loss=torch.mean(losses)
                    if n_step==criterion.log_step: #log the loss
                        log_loss=criterion.log_loss(inputs)
                        epoch_losses.append(torch.mean(log_loss).item())
                    total_loss += loss


                # look a definition of skip_update
                if j % skip_update != 0:
                    idx_max_loss = None

                # backward-pass
                total_loss.backward()

                # normalize gradients
                with torch.no_grad():
                    if normalize_gradients==True:
                        for param in self.parameters():
                            param.grad.data.div_(param.grad.data.norm() + 1e-8)
                    
                optimizer.step()

                # customization of training for the three processes of growing. persisting and regenerating

                # if regenerating, then damage inputs
                if kind == "regenerating" and j % kwargs["skip_damage"] == 0:
                    inputs = inputs.detach()
                    # damages the inputs by removing square portions
                    inputs = make_squares(inputs)

                # if training is not for growing proccess then re-insert trained/damaged samples into the pool
                if kind != "growing":
                    idx_max_loss = n_largest_indexes(log_loss, n_max_losses)
                    pool.update(indexes, inputs, idx_max_loss, evolution_iters)
                    #if we have reset_prob in the kwargs then sometimes the pool resets
                    if 'reset_prob' in kwargs:
                        if np.random.uniform()<kwargs['reset_prob']:
                            pool.reset()
                          

            # update the scheduler if there is one at all
            if scheduler is not None:
                scheduler.step()
            
            # Log epoch losses
            epoch_loss = np.mean(epoch_losses)

            # Stopping criteria
            if np.isnan(epoch_loss) or (epoch_loss > 5 and i > 2):
                print("Stopping early")
                break
            if epoch_loss > 0.25 and epoch == 40: break

            wandb.log({"loss": epoch_loss})
            self.losses.append(epoch_loss)
            print(f"epoch: {epoch+1}\navg loss: {epoch_loss}")
            clear_output(wait=True)

    def plot_losses(self, log_scale=True):
        """Plots the training losses of the model

        Args:
            log_scale (bool, optional): Whether to log scale the loss.
            Defaults to True.
        """
        n = list(range(1, len(self.losses) + 1))
        pl.plot(n, self.losses)
        pl.xlabel("Epochs")
        pl.ylabel("Loss")
        if log_scale:
            pl.yscale("log")
            
        pl.show()

    def update(self,x):
        return 