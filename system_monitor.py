#!/usr/bin/env python3
# -*- coding:utf-8 -*-
"""
System Monitor for 2.13" e-Paper Display
Displays: Time, System Load, and SMART disk statistics
Updates every minute
"""
import sys
import os
import json
import subprocess
import time
import re
from datetime import datetime, timedelta

# Setup paths
epd_base = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'e-Paper/RaspberryPi_JetsonNano/python')
picdir = os.path.join(epd_base, 'pic')
libdir = os.path.join(epd_base, 'lib')
if os.path.exists(libdir):
    sys.path.append(libdir)

import logging
from waveshare_epd import epd2in13
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# State file for tracking SMART deltas
STATE_FILE = os.path.expanduser('~/smart_state.json')
DEVICE = '/dev/sda'


def get_system_load():
    """Read system load averages from /proc/loadavg"""
    try:
        with open('/proc/loadavg', 'r') as f:
            loads = f.read().split()[:3]
            return [float(x) for x in loads]
    except Exception as e:
        logger.error(f"Error reading load: {e}")
        return [0.0, 0.0, 0.0]


def get_smart_stats(device=DEVICE):
    """
    Get SMART statistics from device using smartctl
    Returns dict with: Load_Cycle_Count, Start_Stop_Count,
    Reallocated_Sector_Ct, Current_Pending_Sector,
    Offline_Uncorrectable, UDMA_CRC_Error_Count
    """
    try:
        result = subprocess.run(
            ['sudo', 'smartctl', '-A', device],
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode not in [0, 4]:  # 4 means SMART warning but data is valid
            logger.error(f"smartctl failed: {result.stderr}")
            return None

        stats = {}
        # Parse SMART attributes
        for line in result.stdout.split('\n'):
            line = line.strip()
            if 'Load_Cycle_Count' in line:
                stats['Load_Cycle_Count'] = int(line.split()[9])
            elif 'Start_Stop_Count' in line:
                stats['Start_Stop_Count'] = int(line.split()[9])
            elif 'Reallocated_Sector_Ct' in line:
                stats['Reallocated_Sector_Ct'] = int(line.split()[9])
            elif 'Current_Pending_Sector' in line:
                stats['Current_Pending_Sector'] = int(line.split()[9])
            elif 'Offline_Uncorrectable' in line:
                stats['Offline_Uncorrectable'] = int(line.split()[9])
            elif 'UDMA_CRC_Error_Count' in line:
                stats['UDMA_CRC_Error_Count'] = int(line.split()[9])

        return stats
    except subprocess.TimeoutExpired:
        logger.error("smartctl timeout")
        return None
    except Exception as e:
        logger.error(f"Error reading SMART: {e}")
        return None


def load_state():
    """Load previous SMART state from file"""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, 'r') as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading state: {e}")
    return None


def save_state(stats):
    """Save current SMART state to file with timestamp"""
    try:
        state = {
            'timestamp': datetime.now().isoformat(),
            'stats': stats
        }
        with open(STATE_FILE, 'w') as f:
            json.dump(state, f)
    except Exception as e:
        logger.error(f"Error saving state: {e}")


def calculate_deltas(current_stats, previous_state):
    """Calculate 24h deltas for Load_Cycle_Count and Start_Stop_Count"""
    if not previous_state or not current_stats:
        return None, None

    # Check if previous state is within 24-48 hours
    try:
        prev_time = datetime.fromisoformat(previous_state['timestamp'])
        now = datetime.now()
        age = now - prev_time

        # If state is too old (>48h) or too new (<23h), return None
        if age > timedelta(hours=48) or age < timedelta(hours=23):
            return None, None

        prev_stats = previous_state['stats']
        load_delta = current_stats.get('Load_Cycle_Count', 0) - prev_stats.get('Load_Cycle_Count', 0)
        start_delta = current_stats.get('Start_Stop_Count', 0) - prev_stats.get('Start_Stop_Count', 0)

        return load_delta, start_delta
    except Exception as e:
        logger.error(f"Error calculating deltas: {e}")
        return None, None


def render_display(epd, font18, font36):
    """Render the display with current stats"""
    # Get current data
    current_time = time.strftime('%H:%M')
    loads = get_system_load()
    smart_stats = get_smart_stats()

    # Load previous state and calculate deltas
    prev_state = load_state()
    load_delta, start_delta = calculate_deltas(smart_stats, prev_state)

    # Save current state for next time
    if smart_stats:
        # Check if we should update the saved state (once per day)
        should_save = True
        if prev_state:
            prev_time = datetime.fromisoformat(prev_state['timestamp'])
            if datetime.now() - prev_time < timedelta(hours=23):
                should_save = False

        if should_save:
            save_state(smart_stats)

    # Create image
    image = Image.new('1', (epd.height, epd.width), 255)
    draw = ImageDraw.Draw(image)

    # Left side - Time and Load
    draw.text((0, 0), current_time, font=font36, fill=0)
    draw.text((0, 42), f"{loads[0]:.1f} {loads[1]:.1f} {loads[2]:.1f}", font=font18, fill=0)

    # Right side - SMART stats
    if smart_stats:
        draw.text((125, 0), "/dev/sda:", font=font18, fill=0)

        # Deltas (if available)
        if load_delta is not None:
            draw.text((125, 22), f"LdCyc: +{load_delta}", font=font18, fill=0)
        else:
            draw.text((125, 22), "LdCyc: --", font=font18, fill=0)

        if start_delta is not None:
            draw.text((125, 42), f"StStp: +{start_delta}", font=font18, fill=0)
        else:
            draw.text((125, 42), "StStp: --", font=font18, fill=0)

        # Raw stats
        stats_line = f"{smart_stats.get('Reallocated_Sector_Ct', 0)}|"
        stats_line += f"{smart_stats.get('Current_Pending_Sector', 0)}|"
        stats_line += f"{smart_stats.get('Offline_Uncorrectable', 0)}|"
        stats_line += f"{smart_stats.get('UDMA_CRC_Error_Count', 0)}"
        draw.text((125, 62), "Stats:", font=font18, fill=0)
        draw.text((125, 82), stats_line, font=font18, fill=0)
    else:
        draw.text((125, 25), "SMART N/A", font=font18, fill=0)

    # Rotate image 180 degrees to flip top-to-bottom
    image = image.rotate(180)

    # Display the image
    epd.display(epd.getbuffer(image))


def main():
    """Main loop - updates display every minute"""
    try:
        logger.info("Initializing e-Paper display...")
        epd = epd2in13.EPD()

        # Initial full clear - clear both framebuffers
        epd.init(epd.lut_full_update)

        # Clear both old and new framebuffers (0x24 and 0x26)
        linewidth = int(epd.width / 8) if epd.width % 8 == 0 else int(epd.width / 8) + 1
        epd.SetWindows(0, 0, epd.width, epd.height)
        for j in range(0, epd.height):
            epd.SetCursor(0, j)
            epd.send_command(0x24)  # Write to OLD image RAM
            for i in range(0, linewidth):
                epd.send_data(0xFF)
        for j in range(0, epd.height):
            epd.SetCursor(0, j)
            epd.send_command(0x26)  # Write to NEW image RAM
            for i in range(0, linewidth):
                epd.send_data(0xFF)
        epd.TurnOnDisplay()
        time.sleep(2)

        # Load fonts
        font18 = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), 18)
        font36 = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), 36)

        logger.info("Starting monitoring loop (Ctrl+C to exit)...")

        # Stay in full update mode for darker text
        while True:
            render_display(epd, font18, font36)
            time.sleep(2)

            # Wait 60 seconds
            time.sleep(60)

    except KeyboardInterrupt:
        logger.info("Shutting down...")
        epd.init(epd.lut_full_update)
        epd.Clear(0xFF)
        epd.sleep()
        epd2in13.epdconfig.module_exit(cleanup=True)

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
