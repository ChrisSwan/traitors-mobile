#!/bin/bash
export ANTHROPIC_API_KEY="$1"
cd ~/sdd-projects/traitors-mobile
rm -rf output_test
cat > test_config.json << 'CFGEOF'
{
  "backend": {
    "provider": "claude"
  },
  "run": {
    "num_games": 2,
    "output_dir": "output_test"
  }
}
CFGEOF
python3 -m traitors_sim run-batch --config test_config.json
