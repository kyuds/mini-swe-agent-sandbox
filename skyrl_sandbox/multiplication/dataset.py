"""Preprocess the synthetic 'multiply_sandbox' dataset (parquet).

Port of SkyRL's ``examples/train/multiply/multiply_dataset.py`` with two changes: ``env_class`` is
``multiply_sandbox`` (this repo's sandbox-backed env) and ``extra_info`` carries ``num1``/``num2`` so the
env can recompute the product inside the agent-sandbox pod (see ``env.py``).

    uv run python -m skyrl_sandbox.multiplication.dataset --output_dir ~/data/multiply_sandbox
"""

import argparse
import os
import random

from datasets import Dataset


def generate_multiplication_problem(num_digits):
    """Generate a random multiplication problem with n-digit numbers."""
    min_val = 10 ** (num_digits - 1)
    max_val = 10**num_digits - 1
    num1 = random.randint(min_val, max_val)
    num2 = random.randint(min_val, max_val)
    return num1, num2


def create_dataset(num_examples, num_digits, split_name):
    """Create a dataset of multiplication problems."""
    examples = []
    system_prompt = {
        "role": "system",
        "content": (
            "You are a helpful assistant that solves multiplication problems. Please solve the given "
            "multiplication problem step by step. Put your final answer in \\boxed{answer} format."
        ),
    }
    for _ in range(num_examples):
        num1, num2 = generate_multiplication_problem(num_digits)
        answer = num1 * num2
        examples.append(
            {
                "data_source": "synthetic_multiply",
                "prompt": [system_prompt, {"role": "user", "content": f"{num1} * {num2}"}],
                "env_class": "multiply_sandbox",
                "reward_spec": {"method": "rule", "ground_truth": str(answer)},
                # num1/num2 let the env recompute the product inside the sandbox (the SDK demo).
                "extra_info": {"num1": num1, "num2": num2, "num_digits": num_digits, "split": split_name},
            }
        )
    return Dataset.from_list(examples)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output_dir", default="~/data/multiply_sandbox")
    parser.add_argument("--num_digits", type=int, default=2, help="Digits per operand")
    parser.add_argument("--train_size", type=int, default=10000)
    parser.add_argument("--test_size", type=int, default=200)
    args = parser.parse_args()

    output_dir = os.path.expanduser(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    create_dataset(args.train_size, args.num_digits, "train").to_parquet(os.path.join(output_dir, "train.parquet"))
    create_dataset(args.test_size, args.num_digits, "test").to_parquet(os.path.join(output_dir, "validation.parquet"))
    print(f"Wrote {args.train_size} train + {args.test_size} val ({args.num_digits}-digit) to {output_dir}")
