#!/usr/bin/env python3
"""
Diagnostic script for hass-sip SpeakerSink troubleshooting.
Run this in Home Assistant's shell or SSH to check ffmpeg and audio system setup.
"""
import subprocess
import sys
import os

def run_command(cmd, description):
    """Run a command and report results."""
    print(f"\n{'='*60}")
    print(f"🔍 {description}")
    print(f"{'='*60}")
    print(f"Command: {' '.join(cmd)}")
    print("-" * 60)
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("STDERR:", result.stderr)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print("❌ Command timed out")
        return False
    except FileNotFoundError:
        print(f"❌ Command not found: {cmd[0]}")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False

def main():
    print("🔧 hass-sip SpeakerSink Diagnostic Report")
    print("=" * 60)
    print(f"Python: {sys.version}")
    print(f"OS: {os.name}")
    print(f"Working directory: {os.getcwd()}")
    
    # Check 1: ffmpeg
    print("\n" + "=" * 60)
    print("✅ CHECKS")
    print("=" * 60)
    
    ffmpeg_ok = run_command(["ffmpeg", "-version"], "Check 1: ffmpeg availability")
    
    # Check 2: PulseAudio
    pulse_ok = run_command(
        ["pactl", "list", "sinks"],
        "Check 2: PulseAudio sinks (audio output devices)"
    )
    
    # Check 3: ALSA
    alsa_ok = run_command(
        ["aplay", "-l"],
        "Check 3: ALSA devices"
    )
    
    # Check 4: ffmpeg + PulseAudio test
    print("\n" + "=" * 60)
    print("✅ CHECK 4: ffmpeg + PulseAudio integration test")
    print("=" * 60)
    print("Generating 2-second 1kHz tone and attempting to play it...")
    try:
        # Generate a 2-second 1kHz sine wave
        ffmpeg_cmd = [
            "ffmpeg",
            "-f", "lavfi",
            "-i", "sine=f=1000:d=2",
            "-f", "s16le",
            "-ar", "8000",
            "-"
        ]
        
        pulse_cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel", "error",
            "-f", "s16le",
            "-ar", "8000",
            "-ac", "1",
            "-i", "pipe:0",
            "-f", "pulse",
            "-"
        ]
        
        # Try to pipe audio from generator to pulse
        gen = subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )
        
        player = subprocess.Popen(
            pulse_cmd,
            stdin=gen.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True
        )
        
        gen.stdout.close()
        _, stderr = player.communicate(timeout=3)
        
        if player.returncode == 0:
            print("✅ Successfully piped audio to PulseAudio!")
        else:
            print(f"⚠️  PulseAudio error: {stderr}")
            
    except Exception as e:
        print(f"❌ Test failed: {e}")
    
    # Check 5: Home Assistant logs
    print("\n" + "=" * 60)
    print("✅ CHECK 5: Home Assistant SIP logs (last 20 lines)")
    print("=" * 60)
    log_paths = [
        "/config/home-assistant.log",
        "/var/log/home-assistant/home-assistant.log",
        "/homeassistant/home-assistant.log",
    ]
    
    log_found = False
    for log_path in log_paths:
        if os.path.exists(log_path):
            log_found = True
            print(f"Found log at: {log_path}")
            try:
                result = subprocess.run(
                    ["grep", "-i", "speakersink\\|sip.*audio", log_path],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.stdout:
                    lines = result.stdout.split('\n')[-20:]
                    print('\n'.join(lines))
                else:
                    print("No SpeakerSink or SIP audio logs found. Check full log with:")
                    print(f"  tail -100 {log_path} | grep -i sip")
            except Exception as e:
                print(f"Error reading log: {e}")
            break
    
    if not log_found:
        print("⚠️  Home Assistant log not found in standard locations")
        print("Check your Home Assistant config for log file location")
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 SUMMARY")
    print("=" * 60)
    print(f"✅ ffmpeg: {'Available' if ffmpeg_ok else 'NOT FOUND'}")
    print(f"✅ PulseAudio: {'Available' if pulse_ok else 'NOT FOUND'}")
    print(f"✅ ALSA: {'Available' if alsa_ok else 'NOT FOUND'}")
    
    if not ffmpeg_ok:
        print("\n❌ CRITICAL: ffmpeg is not installed!")
        print("Install with: apt-get install ffmpeg")
    
    if not pulse_ok:
        print("\n⚠️  WARNING: PulseAudio sinks not found!")
        print("Check with: pactl list sinks")
        print("Or install: apt-get install pulseaudio")
    
    if ffmpeg_ok and pulse_ok:
        print("\n✅ Your system is properly configured for SpeakerSink!")
        print("If audio still isn't working, check:")
        print("  1. Home Assistant logs for SpeakerSink errors")
        print("  2. That audio is configured on the HA host speaker")
        print("  3. That incoming RTP audio is actually being received")

if __name__ == "__main__":
    main()
