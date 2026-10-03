#!/usr/bin/env python3
"""
G29 Emulator for Guillemot Force Feedback Racing Wheel (06f8:0004)
Lit les événements du volant Guillemot et les émule en tant que G29.
Nécessite les permissions root (sudo) pour accéder à /dev/input/ et créer uinput.
"""

import os
import sys
import time
import signal
import logging
from evdev import ecodes, InputDevice, UInput, categorize, list_devices

# Configuration du logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("G29Emulator")

# --- CONSTANTES ---
# ID du périphérique source (Guillemot)
GUILLEMOT_VENDOR_ID = 0x06F8
GUILLEMOT_PRODUCT_ID = 0x0004

# Plages des axes
# Guillemot: ABS_WHEEL (code 8) → [-1920, 1920]
# G29: ABS_X (code 0) → [-32767, 32767] (16-bit signed)
WHEEL_MIN_GUILLEMOT = -1920
WHEEL_MAX_GUILLEMOT = 1920
WHEEL_MIN_G29 = -32767
WHEEL_MAX_G29 = 32767

# --- MAPPING DES BOUTONS ---
# Mapping des boutons Guillemot (BTN_BASE*) vers G29 (BTN_SOUTH, etc.)
BUTTON_MAP = {
    294: ecodes.BTN_SOUTH,    # BTN_BASE → A (288)
    295: ecodes.BTN_EAST,     # BTN_BASE2 → B (289)
    296: ecodes.BTN_NORTH,    # BTN_BASE3 → X (290)
    297: ecodes.BTN_WEST,     # BTN_BASE4 → Y (291)
    298: ecodes.BTN_TL,       # BTN_BASE5 → LB (292)
    299: ecodes.BTN_TR,       # BTN_BASE6 → RB (293)
    336: ecodes.BTN_GEAR_DOWN,  # BTN_GEAR_DOWN → Paddle Down
    337: ecodes.BTN_GEAR_UP,    # BTN_GEAR_UP → Paddle Up
}

# --- MAPPING DES AXES ---
# Guillemot → G29
AXIS_MAP = {
    8: 0,   # ABS_WHEEL → ABS_X (volant)
    9: 9,   # ABS_GAS → ABS_GAS (accélérateur)
    10: 10,  # ABS_BRAKE → ABS_BRAKE (frein)
    16: 16,  # ABS_HAT0X → ABS_HAT0X (D-pad X)
    17: 17,  # ABS_HAT0Y → ABS_HAT0Y (D-pad Y)
}

# --- MAPPING DES EFFETS DE FORCE (FF) ---
# Les codes FF sont identiques entre les deux périphériques
FF_EFFECTS = [
    ecodes.FF_RUMBLE,
    ecodes.FF_PERIODIC,
    ecodes.FF_CONSTANT,
    ecodes.FF_SPRING,
    ecodes.FF_DAMPER,
    ecodes.FF_SQUARE,
    ecodes.FF_TRIANGLE,
    ecodes.FF_SINE,
    ecodes.FF_SAW_UP,
    ecodes.FF_SAW_DOWN,
    ecodes.FF_GAIN,
    ecodes.FF_AUTOCENTER,
]


class G29Emulator:
    def __init__(self):
        self.source_device = None
        self.uinput_device = None
        self.running = False
        self.last_axis_values = {}  # Pour éviter les doublons d'événements

    def find_guillemot_device(self):
        """Trouve le périphérique Guillemot (06f8:0004)"""
        devices = [InputDevice(path) for path in list_devices()]
        for device in devices:
            if (
                device.info.vendor == GUILLEMOT_VENDOR_ID
                and device.info.product == GUILLEMOT_PRODUCT_ID
            ):
                logger.info(
                    f"Peripherique Guillemot trouve: {device.name} ({device.path})"
                )
                return device
        raise RuntimeError(
            "Peripherique Guillemot (06f8:0004) introuvable. "
            "Verifiez qu'il est branche et que le driver est charge."
        )

    def create_uinput_device(self):
        """Crée un périphérique virtuel uinput émulant un G29"""
        # Capacités du G29
        capabilities = {
            # Événements de synchronisation
            ecodes.EV_SYN: [0],  # SYN_REPORT
            # Boutons
            ecodes.EV_KEY: list(BUTTON_MAP.values()),
            # Axes
            ecodes.EV_ABS: [
                # Volant (ABS_X)
                (0, ecodes.AbsInfo(
                    min=WHEEL_MIN_G29,
                    max=WHEEL_MAX_G29,
                    fuzz=0,
                    flat=0,
                )),
                # Gaz (ABS_GAS)
                (9, ecodes.AbsInfo(
                    min=0,
                    max=255,
                    fuzz=0,
                    flat=0,
                )),
                # Frein (ABS_BRAKE)
                (10, ecodes.AbsInfo(
                    min=0,
                    max=255,
                    fuzz=0,
                    flat=0,
                )),
                # D-pad X
                (16, ecodes.AbsInfo(
                    min=-1,
                    max=1,
                    fuzz=0,
                    flat=0,
                )),
                # D-pad Y
                (17, ecodes.AbsInfo(
                    min=-1,
                    max=1,
                    fuzz=0,
                    flat=0,
                )),
            ],
            # Effets de force (FF)
            ecodes.EV_FF: FF_EFFECTS,
            ecodes.EV_FF_STATUS: [0],
        }

        # Création du périphérique uinput
        self.uinput_device = UInput(
            capabilities=capabilities,
            name="Logitech G29 Racing Wheel",
            vendor=0x046D,  # Logitech vendor ID
            product=0xC299,  # G29 product ID (exemple)
            version=0x0110,
            bustype=ecodes.BUS_USB,
        )
        logger.info("Peripherique uinput G29 cree avec succes.")

    def map_axis_value(self, axis_code, value):
        """Mappe la valeur d'un axe du Guillemot vers le G29"""
        if axis_code == 8:  # ABS_WHEEL → ABS_X
            # Remappage lineaire : [-1920, 1920] → [-32767, 32767]
            return int(
                (value - WHEEL_MIN_GUILLEMOT)
                * (WHEEL_MAX_G29 - WHEEL_MIN_G29)
                / (WHEEL_MAX_GUILLEMOT - WHEEL_MIN_GUILLEMOT)
                + WHEEL_MIN_G29
            )
        else:
            # Les autres axes (gaz, frein, D-pad) ont les memes plages
            return value

    def handle_event(self, event):
        """Traite un événement du Guillemot et l'envoie au uinput"""
        if event.type == ecodes.EV_SYN:
            # Transmettre SYN_REPORT tel quel
            self.uinput_device.write(ecodes.EV_SYN, ecodes.SYN_REPORT, 0)
            self.uinput_device.syn()
            return

        # Mapper le code de l'evenement
        if event.type == ecodes.EV_KEY:
            if event.code in BUTTON_MAP:
                self.uinput_device.write(ecodes.EV_KEY, BUTTON_MAP[event.code], event.value)
            else:
                # Transmettre les autres boutons sans mapping (ex: BTN_GEAR_DOWN/UP)
                self.uinput_device.write(ecodes.EV_KEY, event.code, event.value)

        elif event.type == ecodes.EV_ABS:
            if event.code in AXIS_MAP:
                mapped_code = AXIS_MAP[event.code]
                mapped_value = self.map_axis_value(event.code, event.value)
                self.uinput_device.write(ecodes.EV_ABS, mapped_code, mapped_value)

        elif event.type == ecodes.EV_FF:
            # Transmettre les effets de force sans modification
            self.uinput_device.write(ecodes.EV_FF, event.code, event.value)

        elif event.type == ecodes.EV_FF_STATUS:
            # Transmettre les statuts FF
            self.uinput_device.write(ecodes.EV_FF_STATUS, event.code, event.value)

    def run(self):
        """Boucle principale de lecture/transmission des evenements"""
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

        self.running = True
        logger.info("Demarrage de l'emulation G29... (Ctrl+C pour arreter)")

        try:
            for event in self.source_device.read_loop():
                if not self.running:
                    break
                self.handle_event(event)
        except Exception as e:
            logger.error(f"Erreur dans la boucle d'evenements: {e}")
        finally:
            self.cleanup()

    def signal_handler(self, signum, frame):
        """Gere les signaux d'arret (Ctrl+C)"""
        logger.info(f"Signal {signum} recu. Arret en cours...")
        self.running = False

    def cleanup(self):
        """Nettoie les ressources"""
        if self.uinput_device:
            self.uinput_device.close()
            logger.info("Peripherique uinput ferme.")
        if self.source_device:
            self.source_device.close()
            logger.info("Peripherique source ferme.")
        self.running = False


def main():
    # Verifier que le script est lance avec sudo
    if os.geteuid() != 0:
        logger.error("Ce script doit etre lance avec sudo (permissions root requises).")
        sys.exit(1)

    try:
        emulator = G29Emulator()
        emulator.source_device = emulator.find_guillemot_device()
        emulator.create_uinput_device()
        emulator.run()
    except Exception as e:
        logger.error(f"Erreur fatale: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
