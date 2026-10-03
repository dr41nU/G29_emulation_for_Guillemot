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
import threading
import glob
import select
import fcntl
from evdev import ecodes, InputDevice, UInput, categorize, list_devices

# Ajouter les constantes FF manquantes (non définies dans evdev 2.0.0)
if not hasattr(ecodes, 'FF_START'):
    ecodes.FF_START = 0x80
if not hasattr(ecodes, 'FF_STOP'):
    ecodes.FF_STOP = 0x81
if not hasattr(ecodes, 'FF_SET_GAIN'):
    ecodes.FF_SET_GAIN = 0x82
if not hasattr(ecodes, 'FF_SET_AUTOCENTER'):
    ecodes.FF_SET_AUTOCENTER = 0x83

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
        self.uinput_input_device = None
        self.uinput_device_node = None
        self.running = False
        self.ff_thread = None
        self.last_axis_values = {}
        self.ff_effect_map = {}
        self.next_guillemot_effect_id = 1

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
        # Vérifier que /dev/uinput existe et est accessible
        import os
        import stat
        
        uinput_path = '/dev/uinput'
        if not os.path.exists(uinput_path):
            # Essayer de créer le device node si le module est chargé
            try:
                os.mknod(uinput_path, stat.S_IFCHR | 0o666, os.makedev(10, 223))
                logger.info("Création de /dev/uinput réussie")
            except Exception as e:
                raise RuntimeError(
                    f"Le périphérique {uinput_path} n'existe pas et ne peut pas être créé. "
                    f"Erreur: {e}. "
                    "Vérifiez que le module uinput est chargé (lsmod | grep uinput) "
                    "et que vous avez les permissions root."
                )
        
        # Vérifier les permissions
        if not os.access(uinput_path, os.R_OK | os.W_OK):
            raise RuntimeError(
                f"Pas de permissions en lecture/écriture sur {uinput_path}. "
                "Exécutez: sudo chmod 666 /dev/uinput"
            )
        
        # Capacités du G29
        # Pour UInput, EV_ABS prend une liste de codes d'axes (pas de tuples)
        abs_axes = [
            ecodes.ABS_X,      # Volant
            ecodes.ABS_GAS,    # Accélérateur
            ecodes.ABS_BRAKE,  # Frein
            ecodes.ABS_HAT0X, # D-pad X
            ecodes.ABS_HAT0Y, # D-pad Y
        ]
        
        # Capacités du G29 avec support Force Feedback
        capabilities = {
            # Boutons (tous les boutons mappés)
            ecodes.EV_KEY: list(BUTTON_MAP.values()),
            # Axes
            ecodes.EV_ABS: abs_axes,
            # Effets de force feedback
            ecodes.EV_FF: [
                ecodes.FF_RUMBLE,
                ecodes.FF_CONSTANT,
                ecodes.FF_SPRING,
                ecodes.FF_DAMPER,
                ecodes.FF_SQUARE,
                ecodes.FF_TRIANGLE,
                ecodes.FF_SINE,
            ],
        }

        # Création du périphérique uinput avec support FF
        try:
            self.uinput_device = UInput(
                events=capabilities,
                name="Logitech G29 Racing Wheel",
                vendor=0x046D,  # Logitech vendor ID
                product=0xC299,  # G29 product ID
                version=0x0110,
                bustype=ecodes.BUS_USB,
                devnode=uinput_path,
                max_effects=96,  # Nombre maximum d'effets simultanés
            )
        except Exception as e:
            # Essayer sans vendor/product/version/bustype si ça échoue
            logger.warning(f"First UInput attempt failed: {e}, trying minimal config...")
            try:
                self.uinput_device = UInput(
                    events=capabilities,
                    name="Logitech G29 Racing Wheel",
                    devnode=uinput_path,
                    max_effects=96,
                )
            except Exception as e2:
                logger.error(f"Failed to create UInput device: {e2}")
                raise RuntimeError(f"Impossible de créer le périphérique uinput: {e2}")
        logger.info("Peripherique uinput G29 cree avec succes.")
        
        # Trouver le device node créé par uinput
        self.find_uinput_device_node()

    def find_uinput_device_node(self):
        """Trouve le device node créé par uinput par son nom"""
        time.sleep(1)  # Attendre que le device soit créé
        
        # Méthode 1: Chercher par nom
        for path in sorted(glob.glob('/dev/input/event*'), key=lambda x: int(x.split('event')[-1]), reverse=True):
            try:
                device = InputDevice(path)
                if device.name == "Logitech G29 Racing Wheel":
                    self.uinput_device_node = path
                    self.uinput_input_device = device
                    logger.info(f"Device node uinput trouve: {self.uinput_device_node}")
                    return
            except Exception:
                continue
        
        # Méthode 2: Chercher par vendor/product
        for path in sorted(glob.glob('/dev/input/event*'), key=lambda x: int(x.split('event')[-1]), reverse=True):
            try:
                device = InputDevice(path)
                if (device.info.vendor == 0x046D and device.info.product == 0xC299):
                    self.uinput_device_node = path
                    self.uinput_input_device = device
                    logger.info(f"Device node uinput trouve par ID: {self.uinput_device_node}")
                    return
            except Exception:
                continue
        
        # Méthode 3: Prendre le dernier device event
        logger.warning("Impossible de trouver le device node uinput par nom/ID, tentative avec le dernier event...")
        event_files = sorted(glob.glob('/dev/input/event*'), key=lambda x: int(x.split('event')[-1]), reverse=True)
        if event_files:
            last_event = event_files[0]
            try:
                self.uinput_device_node = last_event
                self.uinput_input_device = InputDevice(last_event)
                logger.info(f"Device node uinput (dernier): {self.uinput_device_node}")
            except Exception as e:
                logger.error(f"Echec ouverture {last_event}: {e}")
        else:
            logger.error("Aucun device node event trouvé")

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
            # Transmettre les effets de force au périphérique uinput
            try:
                self.uinput_device.write(ecodes.EV_FF, event.code, event.value)
            except Exception as e:
                logger.debug(f"FF write error: {e}")

        elif event.type == ecodes.EV_FF_STATUS:
            # Transmettre les statuts FF
            try:
                self.uinput_device.write(ecodes.EV_FF_STATUS, event.code, event.value)
            except Exception as e:
                logger.debug(f"FF_STATUS write error: {e}")
    
    def handle_ff_command(self, event):
        """Gère les commandes FF reçues sur uinput et les transmet au Guillemot"""
        if event.type == ecodes.EV_FF:
            if event.code in [ecodes.FF_RUMBLE, ecodes.FF_CONSTANT, ecodes.FF_SPRING,
                              ecodes.FF_DAMPER, ecodes.FF_SQUARE, ecodes.FF_TRIANGLE,
                              ecodes.FF_SINE, ecodes.FF_SAW_UP, ecodes.FF_SAW_DOWN]:
                guillemot_effect_id = self.next_guillemot_effect_id
                self.next_guillemot_effect_id += 1
                self.ff_effect_map[event.value] = guillemot_effect_id
                try:
                    self.source_device.write(ecodes.EV_FF, event.code, event.value)
                    self.source_device.syn()
                    logger.debug(f"FF effect created: uinput_id={event.value}, guillemot_id={guillemot_effect_id}")
                except Exception as e:
                    logger.warning(f"Erreur creation effet FF: {e}")
                    
            elif event.code == ecodes.FF_START:
                if event.value in self.ff_effect_map:
                    try:
                        self.source_device.write(ecodes.EV_FF, ecodes.FF_START, self.ff_effect_map[event.value])
                        self.source_device.syn()
                        logger.debug(f"FF effect started: uinput_id={event.value}, guillemot_id={self.ff_effect_map[event.value]}")
                    except Exception as e:
                        logger.warning(f"Erreur demarrage effet FF: {e}")
                        
            elif event.code == ecodes.FF_STOP:
                if event.value in self.ff_effect_map:
                    try:
                        self.source_device.write(ecodes.EV_FF, ecodes.FF_STOP, self.ff_effect_map[event.value])
                        self.source_device.syn()
                        logger.debug(f"FF effect stopped: uinput_id={event.value}, guillemot_id={self.ff_effect_map[event.value]}")
                    except Exception as e:
                        logger.warning(f"Erreur arret effet FF: {e}")
                        
            elif event.code in [ecodes.FF_SET_GAIN, ecodes.FF_SET_AUTOCENTER]:
                try:
                    self.source_device.write(ecodes.EV_FF, event.code, event.value)
                    self.source_device.syn()
                except Exception as e:
                    logger.warning(f"Erreur parametre FF: {e}")
        
        elif event.type == ecodes.EV_FF_STATUS:
            try:
                self.source_device.write(ecodes.EV_FF_STATUS, event.code, event.value)
                self.source_device.syn()
            except Exception as e:
                logger.debug(f"FF_STATUS relay error: {e}")

    def run(self):
        """Boucle principale avec select() pour FF bidirectionnel"""
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)

        self.running = True
        logger.info("Demarrage de l'emulation G29 avec support FF bidirectionnel...")

        # Ouvrir les file descriptors pour select()
        source_fd = self.source_device.fd
        uinput_fd = self.uinput_input_device.fd if self.uinput_input_device else None
        
        # Rendre les FDs non-bloquants
        if uinput_fd:
            fcntl.fcntl(uinput_fd, fcntl.F_SETFL, os.O_NONBLOCK)
        fcntl.fcntl(source_fd, fcntl.F_SETFL, os.O_NONBLOCK)
        
        try:
            while self.running:
                # Préparer les FDs pour select
                read_fds = [source_fd]
                if uinput_fd:
                    read_fds.append(uinput_fd)
                
                # Attendre qu'un des FDs soit prêt en lecture
                try:
                    rlist, _, _ = select.select(read_fds, [], [], 0.1)
                except InterruptedError:
                    break
                
                # Lire depuis le périphérique source (Guillemot)
                if source_fd in rlist:
                    try:
                        for event in self.source_device.read():
                            self.handle_event(event)
                    except (BlockingIOError, OSError):
                        pass  # Aucun événement disponible
                
                # Lire depuis le périphérique uinput (pour les commandes FF)
                if uinput_fd and uinput_fd in rlist:
                    try:
                        for event in self.uinput_input_device.read():
                            self.handle_ff_command(event)
                    except (BlockingIOError, OSError):
                        pass  # Aucun événement disponible
                
                # Petit sleep pour éviter la boucle serrée
                time.sleep(0.001)
        except Exception as e:
            logger.error(f"Erreur dans la boucle principale: {e}")
        finally:
            self.cleanup()

    def signal_handler(self, signum, frame):
        """Gere les signaux d'arret (Ctrl+C)"""
        logger.info(f"Signal {signum} recu. Arret en cours...")
        self.running = False

    def cleanup(self):
        """Nettoie les ressources"""
        self.running = False
        if self.uinput_input_device:
            self.uinput_input_device.close()
        if self.uinput_device:
            self.uinput_device.close()
            logger.info("Peripherique uinput ferme.")
        if self.source_device:
            self.source_device.close()
            logger.info("Peripherique source ferme.")


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
