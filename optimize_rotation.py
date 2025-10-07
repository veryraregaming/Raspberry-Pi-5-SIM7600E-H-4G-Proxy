#!/usr/bin/env python3
"""
IP Rotation Optimizer
Tests different rotation timing configurations to find the optimal settings.
Runs multiple rotations with different wait times and tracks which gives best IP variety.
"""

import yaml
import time
import json
import requests
from datetime import datetime
from pathlib import Path

# Configuration
CONFIG_FILE = Path(__file__).parent / "config.yaml"
API_BASE = "http://127.0.0.1:8088"
RESULTS_FILE = Path(__file__).parent / "optimization_results.json"

# Test configurations (teardown_wait, restart_wait, attempts per config)
TEST_CONFIGS = [
    # (teardown_wait, restart_wait, test_count, description)
    (30, 60, 5, "Fast (1.5min per rotation)"),
    (30, 90, 5, "Quick (2min per rotation)"),
    (60, 120, 5, "Balanced (3min per rotation)"),
    (90, 150, 4, "Moderate (4min per rotation)"),
    (120, 180, 3, "Aggressive (5min per rotation)"),
]

def load_config():
    with open(CONFIG_FILE, 'r') as f:
        return yaml.safe_load(f)

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        yaml.safe_dump(config, f, sort_keys=False)

def get_api_token():
    config = load_config()
    return config.get('api', {}).get('token', '')

def trigger_rotation():
    """Trigger IP rotation via API."""
    token = get_api_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        start_time = time.time()
        response = requests.post(f"{API_BASE}/rotate", headers=headers, timeout=600)
        elapsed = time.time() - start_time
        
        if response.status_code in [200, 400]:  # 400 is "same IP" failure
            data = response.json()
            return {
                'success': data.get('status') == 'success',
                'previous_ip': data.get('previous_ip', 'Unknown'),
                'new_ip': data.get('public_ip', 'Unknown'),
                'elapsed_seconds': round(elapsed, 1),
                'error': data.get('error')
            }
        else:
            return None
    except Exception as e:
        print(f"    ❌ API Error: {e}")
        return None

def get_current_ip():
    """Get current IP from API."""
    token = get_api_token()
    headers = {"Authorization": f"Bearer {token}"}
    
    try:
        response = requests.get(f"{API_BASE}/status", headers=headers, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get('public_ip', 'Unknown')
    except Exception:
        pass
    return 'Unknown'

def update_rotation_config(teardown_wait, restart_wait):
    """Update rotation config with new wait times."""
    config = load_config()
    
    if 'rotation' not in config:
        config['rotation'] = {}
    
    config['rotation']['ppp_teardown_wait'] = teardown_wait
    config['rotation']['ppp_restart_wait'] = restart_wait
    config['rotation']['deep_reset_enabled'] = True
    config['rotation']['deep_reset_method'] = 'at'
    config['rotation']['max_attempts'] = 2
    
    save_config(config)
    print(f"  📝 Updated config: teardown={teardown_wait}s, restart={restart_wait}s")

def test_configuration(teardown_wait, restart_wait, test_count, description):
    """Test a specific configuration multiple times."""
    print(f"\n{'='*70}")
    print(f"🧪 Testing: {description}")
    print(f"   Settings: teardown={teardown_wait}s, restart={restart_wait}s")
    print(f"   Tests: {test_count} rotations")
    print(f"{'='*70}")
    
    # Update config
    update_rotation_config(teardown_wait, restart_wait)
    
    # Wait for orchestrator to reload config
    print("  ⏱️ Waiting 5 seconds for config reload...")
    time.sleep(5)
    
    # Get starting IP
    start_ip = get_current_ip()
    print(f"  📍 Starting IP: {start_ip}")
    
    results = {
        'config': {
            'teardown_wait': teardown_wait,
            'restart_wait': restart_wait,
            'description': description
        },
        'rotations': [],
        'unique_ips': set([start_ip]),
        'total_time': 0,
        'success_count': 0,
        'fail_count': 0
    }
    
    # Run tests
    for i in range(test_count):
        print(f"\n  🔄 Rotation {i+1}/{test_count}")
        
        rotation_result = trigger_rotation()
        
        if rotation_result:
            results['rotations'].append(rotation_result)
            results['total_time'] += rotation_result['elapsed_seconds']
            
            if rotation_result['success']:
                results['success_count'] += 1
                results['unique_ips'].add(rotation_result['new_ip'])
                print(f"    ✅ Success: {rotation_result['previous_ip']} → {rotation_result['new_ip']} ({rotation_result['elapsed_seconds']}s)")
            else:
                results['fail_count'] += 1
                print(f"    ❌ Failed: Same IP ({rotation_result['elapsed_seconds']}s)")
                print(f"       Error: {rotation_result['error']}")
        else:
            print(f"    ❌ Rotation failed completely")
            results['fail_count'] += 1
        
        # Small break between rotations
        if i < test_count - 1:
            print(f"    💤 Cooling down for 10 seconds...")
            time.sleep(10)
    
    # Calculate metrics
    unique_count = len(results['unique_ips'])
    avg_time = results['total_time'] / test_count if test_count > 0 else 0
    success_rate = (results['success_count'] / test_count * 100) if test_count > 0 else 0
    ips_per_hour = (unique_count / (results['total_time'] / 3600)) if results['total_time'] > 0 else 0
    
    results['metrics'] = {
        'unique_ips': unique_count,
        'avg_time_per_rotation': round(avg_time, 1),
        'success_rate': round(success_rate, 1),
        'ips_per_hour': round(ips_per_hour, 2),
        'total_test_time': round(results['total_time'], 1)
    }
    
    # Convert set to list for JSON serialization
    results['unique_ips'] = list(results['unique_ips'])
    
    # Print summary
    print(f"\n  📊 Results:")
    print(f"     Unique IPs: {unique_count}")
    print(f"     Success Rate: {success_rate}%")
    print(f"     Avg Time/Rotation: {avg_time}s ({avg_time/60:.1f} min)")
    print(f"     IPs per Hour: {ips_per_hour:.2f}")
    print(f"     Total Test Time: {results['total_time']/60:.1f} minutes")
    
    return results

def run_optimization():
    """Run full optimization test."""
    print("🚀 IP Rotation Optimizer")
    print("="*70)
    print("This will test different rotation timings to find the optimal settings.")
    print(f"Total configurations to test: {len(TEST_CONFIGS)}")
    
    estimated_time = sum(
        (teardown + restart + 30) * count 
        for teardown, restart, count, _ in TEST_CONFIGS
    )
    print(f"Estimated total time: {estimated_time/60:.1f} minutes ({estimated_time/3600:.1f} hours)")
    print("="*70)
    
    input("\nPress Enter to start optimization... (or Ctrl+C to cancel)")
    
    all_results = {
        'test_date': datetime.now().isoformat(),
        'configurations': []
    }
    
    # Run all test configurations
    for teardown, restart, count, desc in TEST_CONFIGS:
        result = test_configuration(teardown, restart, count, desc)
        all_results['configurations'].append(result)
    
    # Save results
    with open(RESULTS_FILE, 'w') as f:
        json.dump(all_results, f, indent=2)
    
    print(f"\n{'='*70}")
    print("🏁 OPTIMIZATION COMPLETE!")
    print(f"{'='*70}")
    
    # Analyze and recommend
    configs = all_results['configurations']
    
    # Sort by IPs per hour (efficiency)
    by_efficiency = sorted(configs, key=lambda x: x['metrics']['ips_per_hour'], reverse=True)
    
    # Sort by unique IPs (variety)
    by_variety = sorted(configs, key=lambda x: x['metrics']['unique_ips'], reverse=True)
    
    # Sort by success rate
    by_success = sorted(configs, key=lambda x: x['metrics']['success_rate'], reverse=True)
    
    print("\n📈 RANKINGS:")
    print("\n1️⃣ Most Efficient (IPs per Hour):")
    for i, cfg in enumerate(by_efficiency[:3], 1):
        metrics = cfg['metrics']
        config = cfg['config']
        print(f"   {i}. {config['description']}")
        print(f"      {metrics['ips_per_hour']:.2f} IPs/hour, {metrics['unique_ips']} unique IPs, {metrics['success_rate']}% success")
    
    print("\n2️⃣ Most Variety (Unique IPs):")
    for i, cfg in enumerate(by_variety[:3], 1):
        metrics = cfg['metrics']
        config = cfg['config']
        print(f"   {i}. {config['description']}")
        print(f"      {metrics['unique_ips']} unique IPs, {metrics['ips_per_hour']:.2f} IPs/hour, {metrics['success_rate']}% success")
    
    print("\n3️⃣ Most Reliable (Success Rate):")
    for i, cfg in enumerate(by_success[:3], 1):
        metrics = cfg['metrics']
        config = cfg['config']
        print(f"   {i}. {config['description']}")
        print(f"      {metrics['success_rate']}% success, {metrics['unique_ips']} unique IPs, {metrics['ips_per_hour']:.2f} IPs/hour")
    
    # Overall recommendation
    print(f"\n{'='*70}")
    print("💡 RECOMMENDATION:")
    best = by_efficiency[0]
    print(f"\nBest overall config: {best['config']['description']}")
    print(f"Settings:")
    print(f"  ppp_teardown_wait: {best['config']['teardown_wait']}")
    print(f"  ppp_restart_wait: {best['config']['restart_wait']}")
    print(f"\nPerformance:")
    print(f"  {best['metrics']['unique_ips']} unique IPs obtained")
    print(f"  {best['metrics']['ips_per_hour']:.2f} IPs per hour")
    print(f"  {best['metrics']['success_rate']}% success rate")
    print(f"  {best['metrics']['avg_time_per_rotation']/60:.1f} minutes per rotation")
    
    print(f"\n{'='*70}")
    print(f"📄 Full results saved to: {RESULTS_FILE}")
    print(f"{'='*70}")
    
    # Ask if user wants to apply recommended settings
    print(f"\nApply recommended settings to config.yaml?")
    apply = input("Type 'yes' to apply, or Enter to skip: ").strip().lower()
    
    if apply == 'yes':
        update_rotation_config(
            best['config']['teardown_wait'],
            best['config']['restart_wait']
        )
        print("✅ Settings applied! Restart orchestrator with: pm2 restart 4g-proxy-orchestrator")
    else:
        print("Skipped. You can manually apply settings from the report above.")

if __name__ == "__main__":
    try:
        run_optimization()
    except KeyboardInterrupt:
        print("\n\n⚠️ Optimization cancelled by user")
    except Exception as e:
        print(f"\n\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

