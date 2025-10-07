#!/usr/bin/env python3
"""
Check if optimization should run and execute it if needed.
Called by run.sh after system setup.
"""

import yaml
import subprocess
import sys
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "config.yaml"

def should_run_optimization():
    """Check if optimization flag is enabled in config."""
    try:
        with open(CONFIG_FILE, 'r') as f:
            config = yaml.safe_load(f)
        
        return config.get('rotation', {}).get('run_optimization', False)
    except Exception as e:
        print(f"Error reading config: {e}")
        return False

def main():
    if should_run_optimization():
        print("="*70)
        print("🎯 OPTIMIZATION ENABLED")
        print("="*70)
        print("Starting automated rotation optimization...")
        print("This will take ~2 hours to find optimal settings.")
        print("="*70)
        print()
        
        # Run optimizer in auto mode with unbuffered output
        try:
            # Use unbuffered mode for real-time output
            result = subprocess.run(
                [sys.executable, '-u', str(Path(__file__).parent / 'optimize_rotation.py'), '--auto'],
                cwd=Path(__file__).parent,
                check=False,
                env={**subprocess.os.environ, 'PYTHONUNBUFFERED': '1'}
            )
            
            if result.returncode == 0:
                print("\n✅ Optimization complete! Best settings applied.")
                return 0
            else:
                print(f"\n⚠️ Optimization exited with code {result.returncode}")
                return result.returncode
                
        except KeyboardInterrupt:
            print("\n⚠️ Optimization cancelled by user")
            return 1
        except Exception as e:
            print(f"\n❌ Optimization failed: {e}")
            return 1
    else:
        print("ℹ️  Optimization not requested (run_optimization: false)")
        return 0

if __name__ == "__main__":
    sys.exit(main())

