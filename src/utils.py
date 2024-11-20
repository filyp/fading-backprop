import random
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import torch as pt
from RestrictedPython import compile_restricted, safe_globals
from RestrictedPython.Eval import default_guarded_getiter
from RestrictedPython.Guards import guarded_iter_unpack_sequence, safer_getattr
from tensordict import TensorDict

original_stdout = sys.stdout


def set_seeds(seed):
    pt.manual_seed(seed)
    pt.cuda.manual_seed_all(seed)
    pt.backends.cudnn.deterministic = True
    pt.backends.cudnn.benchmark = False
    random.seed(seed)


def repo_root():
    return Path(
        subprocess.check_output(["git", "rev-parse", "--show-toplevel"])
        .decode("utf-8")
        .strip()
    )


def commit_hash():
    return subprocess.check_output(["git", "rev-parse", "HEAD"]).decode("utf-8").strip()


def get_batch(iter, n):
    return pt.cat([next(iter)["input_ids"] for _ in range(n)])


def cross_entropy_loss(output, input_ids):
    return pt.nn.CrossEntropyLoss()(
        output.logits[:, :-1, :].flatten(end_dim=1).to(pt.float32),
        input_ids[:, 1:].flatten(),
    )


def correct_logit_loss(output, input_ids):
    logits = output.logits[:, :-1, :].flatten(end_dim=1).to(pt.float32)
    ids = input_ids[:, 1:].flatten()
    true_logits = logits[pt.arange(len(ids)), ids]
    return true_logits.mean()


def clipped_correct_logit_loss(output, input_ids):
    logits = output.logits[:, :-1, :].flatten(end_dim=1).to(pt.float32)
    ids = input_ids[:, 1:].flatten()
    true_logits = logits[pt.arange(len(ids)), ids]
    return true_logits.clip(min=0).mean()


def load_circuit(circuit_name):
    circ = pt.load(repo_root() / "circuits" / f"{circuit_name}.pt", weights_only=True)
    # this is filled with zero imps, at least for polish
    for name in list(circ.keys()):
        if "embed" in name:
            del circ[name]
    return TensorDict(circ)


def kinda_safe_eval(expr):
    # Create a custom globals dictionary with necessary components
    restricted_globals = dict(safe_globals)
    restricted_globals.update({
        "_getiter_": default_guarded_getiter,
        "_iter_unpack_sequence_": guarded_iter_unpack_sequence,
        "_getattr_": safer_getattr,
        # Add any other necessary functions/variables that your expression needs
        "load_circuit": load_circuit,
    })

    byte_code = compile_restricted(expr, filename="<inline code>", mode="eval")
    return eval(byte_code, restricted_globals)


def get_perplexities(model, batches):
    model.eval()
    with pt.no_grad():
        return [cross_entropy_loss(model(batch), batch).exp() for batch in batches]


def print_perplexities(model, batches, step):
    f_ppl, r_ppl = get_perplexities(model, batches)
    stats = dict(forget=f_ppl, retain=r_ppl)
    print(f"{step:4d} " + " ".join(f"{v:11.2f}" for v in stats.values()))


# class that captures stdout and also appends messages to a list
class Tee:
    def __init__(self):
        self.msgs = []

    def write(self, message):
        self.msgs.append(message)
        original_stdout.write(message)
        original_stdout.flush()

    def flush(self):
        original_stdout.flush()


# for reproducibility save the file state and append output into it
def save_file_and_stdout_open(file_name):
    assert sys.stdout == original_stdout
    folder = repo_root() / "results" / datetime.now().strftime("%Y-%m-%d")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now().strftime('%H-%M-%S')}_{Path(file_name).stem}.py"
    shutil.copy(file_name, path)
    sys.stdout = Tee()
    print('"""')
    print("commit hash: ", commit_hash())
    return path


def save_file_and_stdout_close(path):
    print('"""')
    msgs = sys.stdout.msgs
    sys.stdout = original_stdout
    # prepend the messages to the log file
    old_content = path.read_text()
    path.write_text("".join(msgs) + "\n" + old_content)
