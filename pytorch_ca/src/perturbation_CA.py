import torch
from torchvision.io import write_video
import torchvision.transforms as T

from .utils import *
from .neural_CA import *


class PerturbationCA:
    """Given two CA rules evolves the image pixels using the formula:
    x_t+1 = base_CA(x_t) + new_CA(x_t). To avoid that new_CA overwrites the
    base_CA a L2 loss on the update of new_CA should be used.
    """

    def __init__(self, base_CA: CAModel, new_CA: CAModel):
        """Initializes the model

        Args:
            old_CA (CAModel): base_CA model
            new_CA (CAModel): new_CA model, implements the perturbation
        """
        if base_CA.device != new_CA.device:
            Exception(f"The two CAs are on different devices: " +
                      f"decaying_CA.device: {base_CA.device} and " +
                      f"regenerating_CA.device: {new_CA.device}")

        self.device = base_CA.device
        self.old_CA = base_CA
        self.new_CA = new_CA

        self.new_cells = None
        self.losses = []

    def forward(self, x: torch.Tensor,
                angle: float = 0.,
                step_size: float = 1.) -> torch.Tensor:
        """Single update step of the CA

        Forward pass, computes the new state:
        x_t+1 = base_CA(x_t) + new_CA(x_t)

        Args:
            x (torch.Tensor): Current state
            angle (float, optional): Angle of the update. Defaults to 0.
            step_size (float, optional): Step size of the update. Defaults to 1.

        Returns:
            torch.Tensor: Next state
        """
        pre_life_mask = self.new_CA.get_living_mask(x)

        dx_new = self.new_CA.compute_dx(x, angle, step_size)
        dx_old = self.old_CA.compute_dx(x, angle, step_size)
        x += dx_new + dx_old

        post_life_mask = self.new_CA.get_living_mask(x)
        life_mask = pre_life_mask & post_life_mask

        self.new_cells = dx_new * life_mask.float()

        return x * life_mask.float()

    def train_CA(self,
                 optimizer: torch.optim.Optimizer,
                 criterion: Callable[[torch.Tensor], torch.Tensor],
                 pool: SamplePool,
                 n_epochs: int,
                 scheduler: torch.optim.lr_scheduler._LRScheduler = None,
                 batch_size: int = 4,
                 skip_update: int = 2,
                 evolution_iters: Tuple[int, int] = (50, 60),
                 kind: str = "growing",
                 **kwargs):
        """Trains the CA model

        Args:
            optimizer (torch.optim.Optimizer): Optimizer to use, recommended Adam

            criterion (Callable[[torch.Tensor], torch.Tensor]): Loss function to use

            pool (SamplePool): Sample pool from which to extract the images

            n_epochs (int): Number of epochs to perform, 
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
        """

        self.old_CA.train()
        self.new_CA.train()

        for i in range(n_epochs):
            epoch_losses = []
            for j in range(pool.size // batch_size):
                inputs, indexes = pool.sample(batch_size)
                inputs = inputs.to(self.device)
                optimizer.zero_grad()

                for k in range(randint(*evolution_iters)):
                    inputs = self.forward(inputs)
                    criterion.add_mask(self.new_cells)

                loss, idx_max_loss = criterion(inputs)
                epoch_losses.append(loss.item())
                if j % skip_update != 0:
                    idx_max_loss = None

                loss.backward()
                optimizer.step()

                # if regenerating, then damage inputs
                if kind == "regenerating":
                    inputs = inputs.detach()
                    try:
                        target_size = kwargs['target_size']
                    except KeyError:
                        target_size = None
                    try:
                        constant_side = kwargs['constant_side']
                    except KeyError:
                        constant_side = None
                    inputs = make_squares(
                        inputs, target_size=target_size, constant_side=constant_side)

                if kind != "growing":
                    pool.update(inputs, indexes, idx_max_loss)

            if scheduler is not None:
                scheduler.step()

            self.losses.append(np.mean(epoch_losses))
            print(f"epoch: {i+1}\navg loss: {np.mean(epoch_losses)}")
            clear_output(wait=True)

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

    def make_video(self,
                   init_state: torch.Tensor,
                   n_iters: int,
                   regenerating: bool = False,
                   fname: str = None,
                   rescaling: int = 8,
                   fps: int = 10,
                   **kwargs) -> torch.Tensor:
        """Returns the video (torch.Tensor of size (n_iters, init_state.size()))
            of the evolution of the CA starting from a given initial state

        Args:
            init_state (torch.Tensor, optional): Initial state to evolve.
                Defaults to None, which means a seed state.
            n_iters (int): Number of iterations to evolve the CA
            regenerating (bool, optional): Whether to erase a square portion
                of the image during the video, useful if you want to show
                the regenerating capabilities of the CA. Defaults to False.
            fname (str, optional): File where to save the video.
                Defaults to None.
            rescaling (int, optional): Rescaling factor,
                since the CA is a small image we need to rescale it
                otherwise it will be blurry. Defaults to 8.
            fps (int, optional): Fps of the video. Defaults to 10.
        """

        init_state = init_state.to(self.device)

        # set video visualization features
        video_size = init_state.size()[-1] * rescaling
        video = torch.empty((n_iters, 3, video_size, video_size), device="cpu")
        rescaler = T.Resize((video_size, video_size),
                            interpolation=T.InterpolationMode.NEAREST)

        # evolution
        with torch.no_grad():
            for i in range(n_iters):
                video[i] = RGBAtoRGB(rescaler(init_state))[0].cpu()
                init_state = self.forward(init_state)

                if regenerating:
                    if i == n_iters//3:
                        try:
                            target_size = kwargs['target_size']
                        except KeyError:
                            target_size = None
                        try:
                            constant_side = kwargs['constant_side']
                        except KeyError:
                            constant_side = None

                        init_state = make_squares(
                            init_state, target_size=target_size, constant_side=constant_side)

        if fname is not None:
            write_video(fname, video.permute(0, 2, 3, 1), fps=fps)

        return video

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
                Defaults to False.

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
