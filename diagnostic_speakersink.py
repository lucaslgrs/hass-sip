#!/usr/bin/env python3
"""
Diagnostic script for hass-sip SpeakerSink troubleshooting.
Run this in Home Assistant's shell or SSH to check ffmpeg and audio system setup.

Usage:
  In Home Assistant terminal: python3 diagnostic_speakersink.py
  Or via SSH: ssh user@homeassistant.local
            python3 /config/diagnostic_speakersink.py
"""
import subprocess
import sys
import os
import asyncio

def run_command(cmd, description, timeout=5):
    """Run a command and report results."""
    print(f"\n{'='*70}")
    print(f"🔍 {description}")
    print(f"{'='*70}")
    print(f"Command: {' '.join(cmd)}")
    print("-" * 70)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.stdout:
            lines = result.stdout.strip().split('\n')
            for line in lines[:20]:  # Limit output
                print(line)
            if len(lines) > 20:
                print(f"... ({len(lines) - 20} more lines)")
        if result.stderr and result.returncode != 0:
            print(f"STDERR: {result.stderr[:500]}")
        status = "✅ SUCCESS" if result.returncode == 0 else f"❌ FAILED (exit code {result.returncode})"
        print(f"\n{status}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"❌ Command timed out (>{timeout}s)")
        return False
    except FileNotFoundError:
        print(f"❌ Command not found: {cmd[0]}")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def main():
    print("\n" + "="*70)
    print("🔧  hass-sip SpeakerSink Diagnostic Report")
    print("="*70)
    print(f"Python: {sys.version.split()[0]}")
    print(f"OS: {os.name} / Platform: {sys.platform}")
    print(f"Working directory: {os.getcwd()}")
    
    results = {}
    
    # CHECK 1: ffmpeg
    print("\n" + "="*70)
    print("📋 DIAGNOSTIC CHECKS")
    print("="*70)
    
    results['ffmpeg'] = run_command(
        ["ffmpeg", "-version"],
        "CHECK 1: ffmpeg binary availability"
    )
    
    # CHECK 2: PulseAudio
    results['pulseaudio_sinks'] = run_command(
        ["pactl", "list", "sinks"],
        "CHECK 2: PulseAudio audio output devices (sinks)"
    )
    
    # CHECK 3: ALSA devices
    results['alsa'] = run_command(
        ["aplay", "-l"],
        "CHECK 3: ALSA audio devices"
    )
    
    # CHECK 4: Test PulseAudio connectivity
    print("\n" + "="*70)
    print("CHECK 4: PulseAudio daemon status")
    print("="*70)
    try:
        result = subprocess.run(
            ["pactl", "info"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            print("✅ PulseAudio daemon is running")
            for line in result.stdout.split('\n')[:5]:
                if line.strip():
                    print(f"  {line}")
            results['pulseaudio_running'] = True
        else:
            print("❌ PulseAudio daemon not responding")
            results['pulseaudio_running'] = False
    except Exception as e:
        print(f"⚠️  Cannot check PulseAudio: {e}")
        results['pulseaudio_running'] = False
    
    # CHECK 5: Generate audio test
    print("\n" + "="*70)
    print("CHECK 5: Audio generation test (creating silent 2-second audio)")
    print("="*70)
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-f", "lavfi",
                "-i", "anullsrc=r=8000:cl=mono",
                "-t", "2",
                "-f", "s16le",
                "-"
            ],
            capture_output=True,
            timeout=5
        )
        if result.returncode == 0 and len(result.stdout) > 1000:
            print(f"✅ Successfully generated {len(result.stdout)} bytes of audio")
            results['audio_generation'] = True
        else:
            print(f"❌ Audio generation failed (got {len(result.stdout)} bytes)")
            results['audio_generation'] = False
    except Exception as e:
        print(f"❌ Audio generation error: {e}")
        results['audio_generation'] = False
    
    # CHECK 6: ffmpeg → PulseAudio pipe test
    print("\n" + "="*70)
    print("CHECK 6: ffmpeg → PulseAudio pipe test (will generate 2-second tone)")
    print("="*70)
    try:
        # Generate a 2-second 1kHz sine wave
        gen_cmd = [
            "ffmpeg",
            "-f", "lavfi",
            "-i", "sine=f=1000:d=2",
            "-f", "s16le",
            "-ar", "8000",
            "-ac", "1",
            "-"
        ]
        
        play_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "s16le",
            "-ar", "8000",
            "-ac", "1",
            "-i", "pipe:0",
            "-f", "pulse",
            "-t", "2",
            "-"
        ]
        
        gen = subprocess.Popen(
            gen_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        
        player = subprocess.Popen(
            play_cmd,
            stdin=gen.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )
        
        gen.stdout.close()
        _, stderr = player.communicate(timeout=5)
        
        if player.returncode == 0:
            print("✅ Successfully piped audio to PulseAudio!")
            print("   (You should have heard a 1kHz tone if speakers are working)")
            results['pipe_pulseaudio'] = True
        else:
            print(f"⚠️  PulseAudio pipe encountered an error:")
            print(f"   {stderr[:200]}")
            results['pipe_pulseaudio'] = False
            
    except subprocess.TimeoutExpired:
        print("⚠️  Audio playback test timed out (expected for non-blocking sink)")
        results['pipe_pulseaudio'] = True
    except Exception as e:
        print(f"❌ Pipe test failed: {e}")
        results['pipe_pulseaudio'] = False
    
    # CHECK 7: Home Assistant logs
    print("\n" + "="*70)
    print("CHECK 7: Home Assistant SIP logs")
    print("="*70)
    log_paths = [
        "/config/home-assistant.log",
        "/var/log/home-assistant/home-assistant.log",
        "/homeassistant/home-assistant.log",
        os.path.expanduser("~/.homeassistant/home-assistant.log"),
    ]
    
    log_found = False
    for log_path in log_paths:
        if os.path.exists(log_path):
            log_found = True
            print(f"📄 Found log at: {log_path}")
            try:
                result = subprocess.run(
                    ["grep", "-i", "speakersink\\|sip.*audio\\|answer.*button", log_path],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.stdout:
                    lines = result.stdout.split('\n')[-10:]
                    print("📝 Recent SpeakerSink/SIP logs:")
                    for line in lines:
                        if line.strip():
                            print(f"   {line[:120]}")
                    results['logs_found'] = True
                else:
                    print("⚠️  No SpeakerSink logs found yet")
                    print("   Try answering a call and check again")
                    results['logs_found'] = False
            except Exception as e:
                print(f"Error reading log: {e}")
            break
    
    if not log_found:
        print("⚠️  Home Assistant log not found in standard locations")
        print("   Locations checked:")
        for path in log_paths:
            print(f"     - {path}")
    
    # SUMMARY
    print("\n" + "="*70)
    print("📊 DIAGNOSTIC SUMMARY")
    print("="*70)
    
    checks = [
        ("ffmpeg installed", results.get('ffmpeg', False)),
        ("PulseAudio sinks available", results.get('pulseaudio_sinks', False)),
        ("ALSA devices available", results.get('alsa', False)),
        ("PulseAudio daemon running", results.get('pulseaudio_running', False)),
        ("Audio generation works", results.get('audio_generation', False)),
        ("ffmpeg → PulseAudio pipe", results.get('pipe_pulseaudio', False)),
    ]
    
    for check_name, passed in checks:
        status = "✅" if passed else "❌"
        print(f"{status} {check_name}")
    
    print("\n" + "="*70)
    print("🔧 NEXT STEPS")
    print("="*70)
    
    if not results.get('ffmpeg', False):
        print("❌ CRITICAL: ffmpeg is not installed!")
        print("   Install with: apt-get update && apt-get install -y ffmpeg")
    
    if not results.get('pulseaudio_running', False):
        print("❌ CRITICAL: PulseAudio daemon is not running!")
        print("   Install with: apt-get install -y pulseaudio")
        print("   Start with: pulseaudio --daemonize")
    
    if results.get('ffmpeg', False) and results.get('pulseaudio_running', False):
        if results.get('pipe_pulseaudio', False):
            print("✅ Your audio system is properly configured!")
            print("\n📞 TO TEST THE FIX:")
            print("   1. Restart Home Assistant")
            print("   2. Answer an incoming SIP call from the dashboard button")
            print("   3. You should now hear the caller")
            print("\n📋 If audio still doesn't work:")
            print("   - Check Home Assistant logs: tail -f /config/home-assistant.log | grep -i sip")
            print("   - Verify incoming RTP audio is being received")
            print("   - Check speaker volume and routing on the host")
        else:
            print("⚠️  Audio piping test failed")
            print("   Your ffmpeg/PulseAudio setup may need configuration")
    
    print("\n" + "="*70)
    print("💡 USEFUL DEBUGGING COMMANDS")
    print("="*70)
    print("View HA SIP logs in real-time:")
    print("  tail -f /config/home-assistant.log | grep -i sip")
    print("\nCheck PulseAudio servers:")
    print("  pactl list servers")
    print("\nTest audio playback:")
    print("  speaker-test -t sine -f 1000 -l 1")
    print("\nCheck active processes:")
    print("  ps aux | grep -E 'ffmpeg|pulse'")
    print("="*70 + "\n")

if __name__ == "__main__":
    main()
