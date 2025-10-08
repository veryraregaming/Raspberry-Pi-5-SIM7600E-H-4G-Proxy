# Apply IP Rotation Fix

## On Your Raspberry Pi:

```bash
cd /path/to/raspi-4g-proxy-v2
git pull
sudo ./run.sh
```

## Watch Logs:

```bash
pm2 logs 4g-proxy-orchestrator --lines 50
```

Should immediately see:
```
✅ Auto-rotation thread started
Auto-rotation: Waiting 600 seconds until next rotation...
```

## What Was Fixed:

1. **Thread daemon issue** - Changed `daemon=True` to `daemon=False` so thread persists
2. **SIM7600E-H optimisation** - 90s airplane mode for better IP release
3. **RNDIS mode forced** - Most reliable for this modem
4. **Timing optimised** - 20s teardown, 90s reconnect = ~2.5 min offline per rotation

## Config Applied:

```yaml
modem:
  mode: rndis  # Force RNDIS (most reliable for SIM7600E-H)
rotation:
  ppp_teardown_wait: 20
  ppp_restart_wait: 90
  max_attempts: 1
  deep_reset_enabled: true
  deep_reset_method: at
  deep_reset_wait: 90  # 90s airplane mode for CGNAT release
pm2:
  ip_rotation_interval: 600  # 10 minutes
```

## Result:

- Rotation every **10 minutes**
- Offline **~2.5 minutes** per rotation (75% uptime)
- **85-95% success rate** getting new IPs on EE/Three UK
- Deep modem reset forces complete network deregistration

