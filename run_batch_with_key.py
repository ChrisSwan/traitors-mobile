import os
import sys
from pathlib import Path

# Read API key from env (will be set in the script)
api_key = os.environ.get("ANTHROPIC_API_KEY")
if not api_key:
    print("Error: ANTHROPIC_API_KEY not in environment", file=sys.stderr)
    sys.exit(1)

# Run the batch
os.chdir(Path(__file__).parent)
os.system("rm -rf output_test")

config_path = "test_config.json"
Path(config_path).write_text('''{
  "backend": {
    "provider": "claude"
  },
  "run": {
    "num_games": 2,
    "output_dir": "output_test"
  }
}''')

os.environ["ANTHROPIC_API_KEY"] = api_key
os.system(f"{sys.executable} -m traitors_sim run-batch --config {config_path}")
