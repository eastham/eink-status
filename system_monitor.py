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
epd_base = os.path.join(os.path.dirname(os.path.realpath(__file__)), 
                        'e-Paper/RaspberryPi_JetsonNano/python')
picdir = os.path.join(epd_base, 'pic')
libdir = os.path.join(epd_base, 'lib')
if os.path.exists(libdir):
    sys.path.append(libdir)

import logging
from waveshare_epd import epd2in13_V4, epdconfig
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DEVICE = '/dev/sda'

# In-memory rolling 24-hour window for SMART stats (hourly buckets)
smart_stats_history = {}

def get_system_load():
    """Read system load averages from /proc/loadavg"""
    try:
        with open('/proc/loadavg', 'r') as f:
            loads = f.read().split()[:3]
            return [float(x) for x in loads]
    except Exception as e:
        logger.error(f"Error reading load: {e}")
        return [0.0, 0.0, 0.0]


def get_memory_usage():
    """Get memory usage percentage"""
    try:
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
            mem_total = int([l for l in lines if l.startswith('MemTotal:')][0].split()[1])
            mem_available = int([l for l in lines if l.startswith('MemAvailable:')][0].split()[1])
            mem_used_percent = ((mem_total - mem_available) / mem_total) * 100
            return mem_used_percent
    except Exception as e:
        logger.error(f"Error reading memory: {e}")
        return 0.0


def get_cpu_temperature():
    """Get CPU temperature in Celsius"""
    try:
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp = int(f.read().strip()) / 1000.0
            return temp
    except Exception as e:
        logger.error(f"Error reading CPU temp: {e}")
        return 0.0


def get_uptime():
    """Get system uptime as days and hours"""
    try:
        with open('/proc/uptime', 'r') as f:
            uptime_seconds = float(f.read().split()[0])
            days = int(uptime_seconds // 86400)
            hours = int((uptime_seconds % 86400) // 3600)
            return days, hours
    except Exception as e:
        logger.error(f"Error reading uptime: {e}")
        return 0, 0


def get_disk_usage():
    """Get disk usage percentage for main partitions"""
    partitions = {}
    try:
        # Read from df command for accuracy
        result = subprocess.run(
            ['df', '-h'],
            capture_output=True,
            text=True,
            timeout=5
        )

        for line in result.stdout.split('\n')[1:]:  # Skip header
            if line.strip():
                parts = line.split()
                if len(parts) >= 6:
                    device = parts[0]
                    mount = parts[5]
                    used_percent = parts[4].rstrip('%')
                    try:
                        # Store by device name for /dev/sdX1, or by mount point for /
                        if device in ['/dev/sda1', '/dev/sdb1']:
                            partitions[device] = int(used_percent)
                        elif mount == '/':
                            partitions['/'] = int(used_percent)
                    except ValueError:
                        pass

        return partitions
    except Exception as e:
        logger.error(f"Error reading disk usage: {e}")
        return {}


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


def update_smart_history(stats, hour):
    """Add current SMART stats to the hourly bucket (0-23)"""
    if stats:
        smart_stats_history[hour] = stats


def calculate_deltas(current_stats, current_hour):
    """Calculate delta using 24hr old data, or oldest available if not present"""
    if not current_stats or not smart_stats_history:
        return None, None

    try:
        # Prefer data from current hour (which has 24hr old data before we overwrite it)
        if current_hour in smart_stats_history:
            prev_stats = smart_stats_history[current_hour]
        else:
            # Find oldest hour by checking backwards from current hour
            oldest_hour = None
            max_hours_back = 24
            for i in range(1, max_hours_back):
                check_hour = (current_hour - i) % 24
                if check_hour in smart_stats_history:
                    oldest_hour = check_hour

            if oldest_hour is None:
                return None, None

            prev_stats = smart_stats_history[oldest_hour]

        load_delta = current_stats.get('Load_Cycle_Count', 0) - prev_stats.get('Load_Cycle_Count', 0)
        start_delta = current_stats.get('Start_Stop_Count', 0) - prev_stats.get('Start_Stop_Count', 0)
        return load_delta, start_delta
    except Exception as e:
        logger.error(f"Error calculating deltas: {e}")
        return None, None


def render_display(epd, font18, font36, use_partial=True, set_base=False):
    """Render the display with current stats"""
    # Get current data
    current_time = time.strftime('%-I:%M')  # H:MM format 12-hour (no leading zero on hour)
    current_date = time.strftime('%a %b %d')  # Day-of-week + date
    loads = get_system_load()
    mem_percent = get_memory_usage()
    cpu_temp = get_cpu_temperature()
    days, hours = get_uptime()
    disk_usage = get_disk_usage()
    smart_stats = get_smart_stats()

    # Calculate deltas and update history
    current_hour = datetime.now().hour
    load_delta, start_delta = calculate_deltas(smart_stats, current_hour)

    # Update history with current stats (overwrites data from 24 hours ago)
    if smart_stats:
        update_smart_history(smart_stats, current_hour)

    # Create image
    image = Image.new('1', (epd.height, epd.width), 255)
    draw = ImageDraw.Draw(image)

    # Left side - Time, Date, and System Stats
    y_pos = 0

    # Clear and draw time (fixed width: 5 chars "HH:MM")
    draw.rectangle([(1, y_pos + 1), (119, y_pos + 36)], fill=255)
    draw.text((4, y_pos + 1), current_time, font=font36, fill=0)
    y_pos += 38

    # Clear and draw date
    draw.rectangle([(1, y_pos), (119, y_pos + 17)], fill=255)
    draw.text((7, y_pos), current_date, font=font18, fill=0)
   
    # Draw box around time and date
    draw.rectangle([(0, 0), (100, 59)], outline=0, width=1)
    y_pos += 22

    # Clear and draw load
    draw.rectangle([(0, y_pos+1), (120, y_pos + 18)], fill=255)
    draw.text((0, y_pos), f"{loads[0]:.1f} {loads[1]:.1f} {loads[2]:.1f}", font=font18, fill=0)
    y_pos += 20

    # Clear and draw mem/cpu (fixed width formatting)
    draw.rectangle([(0, y_pos), (120, y_pos + 18)], fill=255)
    draw.text((0, y_pos), f"M:{mem_percent:3.0f}% C:{cpu_temp:2.0f}C", font=font18, fill=0)
    y_pos += 20

    # Clear and draw uptime (fixed width: "Up:XXXd XXh")
    draw.rectangle([(0, y_pos), (120, y_pos + 18)], fill=255)
    draw.text((0, y_pos), f"Up:{days:3d}d {hours:2d}h", font=font18, fill=0)

    # Right side - Disk usage and SMART stats
    y_pos = 0

    # Disk usage on right side
    draw.rectangle([(125, y_pos), (250, y_pos + 18)], fill=255)
    if '/' in disk_usage:
        draw.text((125, y_pos), f"/:        {disk_usage['/']:3d}%", font=font18, fill=0)
    y_pos += 20

    draw.rectangle([(125, y_pos), (250, y_pos + 18)], fill=255)
    if '/dev/sda1' in disk_usage:
        draw.text((125, y_pos), f"sda1: {disk_usage['/dev/sda1']:3d}%", font=font18, fill=0)
    y_pos += 20

    draw.rectangle([(125, y_pos), (250, y_pos + 18)], fill=255)
    if '/dev/sdb1' in disk_usage:
        draw.text((125, y_pos), f"sdb1: {disk_usage['/dev/sdb1']:3d}%", font=font18, fill=0)
    y_pos += 20

    # SMART stats
    if smart_stats:
        # Clear and draw Load Cycle delta (fixed width)
        draw.rectangle([(125, y_pos), (250, y_pos + 18)], fill=255)
        if load_delta is not None:
            draw.text((125, y_pos), f"LdCyc: {load_delta}", font=font18, fill=0)
        else:
            draw.text((125, y_pos), "LdCyc: --", font=font18, fill=0)
        y_pos += 20

        # Clear and draw Start/Stop delta (fixed width)
        draw.rectangle([(125, y_pos), (250, y_pos + 18)], fill=255)
        if start_delta is not None:
            draw.text((125, y_pos), f"StStp:  {start_delta}", font=font18, fill=0)
        else:
            draw.text((125, y_pos), "StStp: --", font=font18, fill=0)
        y_pos += 20

        # Clear and draw health stats (all on one line)
        draw.rectangle([(125, y_pos), (250, y_pos + 18)], fill=255)
        stats_line = f"Htlh: {smart_stats.get('Reallocated_Sector_Ct', 0)} "
        stats_line += f"{smart_stats.get('Current_Pending_Sector', 0)} "
        stats_line += f"{smart_stats.get('Offline_Uncorrectable', 0)} "
        stats_line += f"{smart_stats.get('UDMA_CRC_Error_Count', 0)}"
        draw.text((125, y_pos), stats_line, font=font18, fill=0)
    else:
        draw.text((125, y_pos), "SMART N/A", font=font18, fill=0)

    # Rotate image 180 degrees to flip top-to-bottom
    image = image.rotate(180)

    # Display the image using partial or full update
    buffer = epd.getbuffer(image)
    if set_base:
        # Set base image for partial updates (writes to both RAM buffers)
        epd.displayPartBaseImage(buffer)
    elif use_partial:
        epd.displayPartial(buffer)
    else:
        epd.display(buffer)


def main():
    """Main loop - updates display every minute with partial refresh"""
    try:
        logger.info("Initializing e-Paper display...")
        epd = epd2in13_V4.EPD()

        # Initial full clear
        logger.info("Performing initial full clear...")
        epd.init()
        epd.Clear(0xFF)
        time.sleep(1)

        # Load fonts
        font18 = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), 18)
        font36 = ImageFont.truetype(os.path.join(picdir, 'Font.ttc'), 36)

        # Set base image for partial updates
        logger.info("Setting base image for partial updates...")
        render_display(epd, font18, font36, use_partial=False, set_base=True)
        time.sleep(2)

        logger.info("Starting monitoring loop with partial updates (Ctrl+C to exit)...")

        update_count = 0
        while True:
            # Sleep until the top of the next minute for accurate clock sync
            now = time.time()
            seconds_into_minute = now % 60
            sleep_time = 60 - seconds_into_minute
            time.sleep(sleep_time+1)

            # Every 60 updates (1 hour), do a full refresh to clear ghosting
            if update_count % 60 == 0 and update_count > 0:
                logger.info("Performing periodic full refresh...")
                epd.init()
                render_display(epd, font18, font36, use_partial=False)
                epd.init_fast()
            else:
                # Partial update
                render_display(epd, font18, font36, use_partial=True)

            update_count += 1

    except KeyboardInterrupt:
        logger.info("Shutting down...")
        epd.init()
        epd.Clear(0xFF)
        epd.sleep()
        epdconfig.module_exit(cleanup=True)

    except Exception as e:
        logger.error(f"Error: {e}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    main()
