"""
sys_patch_helpers.py: Additional support functions for sys_patch.py
"""

import os
import logging
import plistlib
import subprocess
import struct
import shutil
import tempfile

from typing import Union
from pathlib import Path
from datetime import datetime

from .. import constants

from ..datasets import os_data
from ..volume   import generate_copy_arguments

from ..support import (
    generate_smbios,
    subprocess_wrapper
)


class SysPatchHelpers:
    """
    Library of helper functions for sys_patch.py and related libraries
    """

    def __init__(self, global_constants: constants.Constants):
        self.constants: constants.Constants = global_constants


    def snb_board_id_patch(self, source_files_path: str):
        """
        Patch AppleIntelSNBGraphicsFB.kext to support unsupported Board IDs

        AppleIntelSNBGraphicsFB hard codes the supported Board IDs for Sandy Bridge iGPUs
        Because of this, the kext errors out on unsupported systems
        This function simply patches in a supported Board ID, using 'determine_best_board_id_for_sandy()'
        to supplement the ideal Board ID

        Parameters:
            source_files_path (str): Path to the source files

        """

        source_files_path = str(source_files_path)

        if self.constants.computer.reported_board_id in self.constants.sandy_board_id_stock:
            return

        logging.info(f"Found unsupported Board ID {self.constants.computer.reported_board_id}, performing AppleIntelSNBGraphicsFB bin patching")

        board_to_patch = generate_smbios.determine_best_board_id_for_sandy(self.constants.computer.reported_board_id, self.constants.computer.gpus)
        logging.info(f"Replacing {board_to_patch} with {self.constants.computer.reported_board_id}")

        board_to_patch_hex = bytes.fromhex(board_to_patch.encode('utf-8').hex())
        reported_board_hex = bytes.fromhex(self.constants.computer.reported_board_id.encode('utf-8').hex())

        if len(board_to_patch_hex) > len(reported_board_hex):
            # Pad the reported Board ID with zeros to match the length of the board to patch
            reported_board_hex = reported_board_hex + bytes(len(board_to_patch_hex) - len(reported_board_hex))
        elif len(board_to_patch_hex) < len(reported_board_hex):
            logging.info(f"Error: Board ID {self.constants.computer.reported_board_id} is longer than {board_to_patch}")
            raise Exception("Host's Board ID is longer than the kext's Board ID, cannot patch!!!")

        path = source_files_path + "/10.13.6/System/Library/Extensions/AppleIntelSNBGraphicsFB.kext/Contents/MacOS/AppleIntelSNBGraphicsFB"
        if not Path(path).exists():
            logging.info(f"Error: Could not find {path}")
            raise Exception("Failed to find AppleIntelSNBGraphicsFB.kext, cannot patch!!!")

        with open(path, 'rb') as f:
            data = f.read()
            data = data.replace(board_to_patch_hex, reported_board_hex)
            with open(path, 'wb') as f:
                f.write(data)


    def generate_patchset_plist(self, patchset: dict, file_name: str, kdk_used: Path, metallib_used: Path):
        """
        Generate patchset file for user reference

        Parameters:
            patchset (dict): Dictionary of patchset, sys_patch/patchsets
            file_name (str): Name of the file to write to
            kdk_used (Path): Path to the KDK used, if any

        Returns:
            bool: True if successful, False if not

        """

        source_path = f"{self.constants.payload_path}"
        source_path_file = f"{source_path}/{file_name}"

        kdk_string = "Not applicable"
        if kdk_used:
            kdk_string = kdk_used

        metallib_used_string = "Not applicable"
        if metallib_used:
            metallib_used_string = metallib_used

        data = {
            "OpenCore Legacy Patcher": f"v{self.constants.patcher_version}",
            "PatcherSupportPkg": f"v{self.constants.patcher_support_pkg_version}",
            "Time Patched": f"{datetime.now().strftime('%B %d, %Y @ %H:%M:%S')}",
            "Commit URL": f"{self.constants.commit_info[2]}",
            "Kernel Debug Kit Used": f"{kdk_string}",
            "Metal Library Used": f"{metallib_used_string}",
            "OS Version": f"{self.constants.detected_os}.{self.constants.detected_os_minor} ({self.constants.detected_os_build})",
            "Custom Signature": bool(Path(self.constants.payload_local_binaries_root_path / ".signed").exists()) and not (
                "AMD Legacy GCN" in patchset and self.constants.detected_os >= os_data.os_data.sequoia
            ),
        }

        data.update(patchset)

        if Path(source_path_file).exists():
            os.remove(source_path_file)

        # Need to write to a safe location
        plistlib.dump(data, Path(source_path_file).open("wb"), sort_keys=False)

        if Path(source_path_file).exists():
            return True

        return False


    def disable_window_server_caching(self):
        """
        Disable WindowServer's asset caching

        On legacy GCN GPUs, the WindowServer cache generated creates
        corrupted Opaque shaders.

        To work-around this, we disable WindowServer caching
        And force macOS into properly generating the Opaque shaders
        """

        if self.constants.detected_os < os_data.os_data.ventura:
            return

        logging.info("Disabling WindowServer Caching")
        # Invoke via 'bash -c' to resolve pathing
        subprocess_wrapper.run_as_root(["/bin/bash", "-c", "/bin/rm -rf /private/var/folders/*/*/*/WindowServer/com.apple.WindowServer"])
        # Disable writing to WindowServer folder
        subprocess_wrapper.run_as_root(["/bin/bash", "-c", "/usr/bin/chflags uchg /private/var/folders/*/*/*/WindowServer"])
        # Reference:
        #   To reverse write lock:
        #   'chflags nouchg /private/var/folders/*/*/*/WindowServer'


    def install_rsr_repair_binary(self):
        """
        Installs RSRRepair

        RSRRepair is a utility that will sync the SysKC and BootKC in the event of a panic

        With macOS 13.2, Apple implemented the Rapid Security Response System
        However Apple added a half baked snapshot reversion system if seal was broken,
        which forgets to handle Preboot BootKC syncing.

        Thus this application will try to re-sync the BootKC with SysKC in the event of a panic
            Reference: https://github.com/dortania/OpenCore-Legacy-Patcher/issues/1019

        This is a (hopefully) temporary work-around, however likely to stay.
        RSRRepair has the added bonus of fixing desynced KCs from 'bless', so useful in Big Sur+
            Source: https://github.com/flagersgit/RSRRepair

        """

        if self.constants.detected_os < os_data.os_data.big_sur:
            return

        logging.info("Installing Kernel Collection syncing utility")
        result = subprocess_wrapper.run_as_root([self.constants.rsrrepair_userspace_path, "--install"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if result.returncode != 0:
            logging.info("- Failed to install RSRRepair")
            subprocess_wrapper.log(result)


    def patch_gpu_compiler_libraries(self, mount_point: Union[str, Path]):
        """
        Fix GPUCompiler.framework's libraries to resolve linking issues

        On 13.3 with 3802 GPUs, OCLP will downgrade GPUCompiler to resolve
        graphics support. However the binary hardcodes the library names,
        and thus we need to adjust the libraries to match (31001.669)

        Important portions of the library will be downgraded to 31001.669,
        and the remaining bins will be copied over (via CoW to reduce waste)

        Primary folders to merge:
        - 31001.XXX: (current OS version)
            - include:
                - module.modulemap
                - opencl-c.h
            - lib (entire directory)

        Note: With macOS Sonoma, 32023 compiler is used instead and so this patch is not needed
              until macOS 14.2 Beta 2 with version '32023.26'.

        Parameters:
            mount_point: The mount point of the target volume
        """
        if os_data.os_data.sonoma < self.constants.detected_os < os_data.os_data.ventura:
            return

        if self.constants.detected_os == os_data.os_data.ventura:
            if self.constants.detected_os_minor < 4: # 13.3
                return
            BASE_VERSION = "31001"
            GPU_VERSION = f"{BASE_VERSION}.669"
        elif self.constants.detected_os == os_data.os_data.sonoma:
            if self.constants.detected_os_minor < 2: # 14.2 Beta 2
                return
            BASE_VERSION = "32023"
            GPU_VERSION = f"{BASE_VERSION}.26"
        else:
            # Fall back for newer versions
            BASE_VERSION = "32023"
            GPU_VERSION = f"{BASE_VERSION}.26"

        LIBRARY_DIR = f"{mount_point}/System/Library/PrivateFrameworks/GPUCompiler.framework/Versions/{BASE_VERSION}/Libraries/lib/clang"
        DEST_DIR = f"{LIBRARY_DIR}/{GPU_VERSION}"

        if not Path(DEST_DIR).exists():
            raise Exception(f"Failed to find GPUCompiler libraries at {DEST_DIR}")

        for file in Path(LIBRARY_DIR).iterdir():
            if file.is_file():
                continue
            if file.name == GPU_VERSION:
                continue

            # Partial match as each OS can increment the version
            if not file.name.startswith(f"{BASE_VERSION}."):
                continue

            logging.info(f"Merging GPUCompiler.framework libraries to match binary")

            src_dir = f"{LIBRARY_DIR}/{file.name}"
            if not Path(f"{DEST_DIR}/lib").exists():
                subprocess_wrapper.run_as_root_and_verify(generate_copy_arguments(f"{src_dir}/lib", f"{DEST_DIR}/"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

            break


    def patch_amd_legacy_gcn(self, mount_point: Union[str, Path]) -> None:
        """
        Fix AMD Legacy GCN driver crashes in macOS 15 Sequoia:
        1. libAMDIL902.dylib: Null-pointer dereference in initializeIABArgTypeSet (0x7DBFE -> 0xAD0430 cave)
        2. AMDMTLBronzeDriver: Inject LC_LOAD_DYLIB for @loader_path/libAMDFix.dylib
        3. libAMDFix.dylib: Companion helper for reflection unwrapping and IAB alignment/constant data sink
        """
        if self.constants.detected_os < os_data.os_data.sequoia:
            return

        logging.info("Applying AMD Legacy GCN Metal and compiler stability patches")

        mount_point = Path(mount_point)
        il_dest = mount_point / "System/Library/Extensions/AMDShared.bundle/Contents/PlugIns/libAMDIL902.dylib"
        drv_dir = mount_point / "System/Library/Extensions/AMDMTLBronzeDriver.bundle/Contents/MacOS"
        drv_dest = drv_dir / "AMDMTLBronzeDriver"
        fix_dest = drv_dir / "libAMDFix.dylib"
        fix_source = self.constants.amd_fix_path
        if not fix_source.exists():
            fix_source = self.constants.current_path / "payloads" / "libAMDFix.dylib"
        if not fix_source.exists():
            fix_source = Path(__file__).resolve().parents[2] / "payloads" / "libAMDFix.dylib"

        def _rel_call(src: int, dst: int) -> bytes:
            offset = (dst - (src + 5)) & 0xFFFFFFFF
            return b'\xE8' + struct.pack('<I', offset)

        def _rel_jmp(src: int, dst: int) -> bytes:
            offset = (dst - (src + 5)) & 0xFFFFFFFF
            return b'\xE9' + struct.pack('<I', offset)

        def _write_root_file(data: bytes, dest_path: Path) -> None:
            with tempfile.NamedTemporaryFile(delete=False) as temp_file:
                temp_file.write(data)
                temp_path = temp_file.name
            try:
                subprocess_wrapper.run_as_root_and_verify(["/bin/cp", "-f", temp_path, str(dest_path)])
                subprocess_wrapper.run_as_root_and_verify(["/bin/chmod", "755", str(dest_path)])
                subprocess_wrapper.run_as_root_and_verify(["/usr/sbin/chown", "root:wheel", str(dest_path)])
            finally:
                Path(temp_path).unlink(missing_ok=True)

        # 1. Patch libAMDIL902.dylib
        if il_dest.exists():
            logging.info(f"- In-place patching {il_dest.name} for safe null-filter")
            with open(il_dest, 'rb') as f:
                il_data = bytearray(f.read())

            if not (len(il_data) > 0xAD0430 and il_data[0x7DBFE] == 0xE9 and il_data[0xAD0430] == 0x4C):
                cave_va = 0x4ffa0de40430
                cave_offset = 0xAD0430
                il_data[cave_offset:cave_offset + 100] = bytes([0x00] * 100)

                cave_code = bytearray()
                cave_code += bytes.fromhex('4c8d7db8')              # leaq -0x48(%rbp), %r15
                cave_code += bytes.fromhex('4c8da518ffffff')        # leaq -0xe8(%rbp), %r12
                cave_code += bytes.fromhex('488b8080000000')        # movq 0x80(%rax), %rax
                cave_code += bytes.fromhex('4885c0')                # testq %rax, %rax
                cave_code += bytes.fromhex('740e')                  # je skip (+14 bytes)
                cave_code += bytes.fromhex('488b10')                # movq (%rax), %rdx
                cave_code += bytes.fromhex('4c89ff')                # movq %r15, %rdi
                cave_code += bytes.fromhex('4c89e6')                # movq %r12, %rsi
                call_pc = cave_va + len(cave_code)
                cave_code += _rel_call(call_pc, 0x4ffa0d3edc82)     # callq insert (0x7DC82)
                jmp_pc = cave_va + len(cave_code)
                cave_code += _rel_jmp(jmp_pc, 0x4ffa0d3edc1e)       # jmp back to 0x7DC1E

                il_data[cave_offset:cave_offset + len(cave_code)] = cave_code

                hook1_va = 0x4ffa0d3edbfe
                hook1_offset = 0x7DBFE
                hook1 = _rel_jmp(hook1_va, cave_va) + (b'\x90' * (32 - 5))
                il_data[hook1_offset:hook1_offset + 32] = hook1

                p2_loc = 0x7E9A2
                p2_patch = bytes.fromhex('4d85e4742e41833c24007527eb0f')
                il_data[p2_loc:p2_loc + len(p2_patch)] = p2_patch

                p3_loc = 0x7F851
                p3_patch = bytes.fromhex('4d85ed742b41837d00007524')
                il_data[p3_loc:p3_loc + len(p3_patch)] = p3_patch

                loc_run = 0x7DCDA
                orig_run = bytes([0x55, 0x48, 0x89, 0xe5])
                if il_data[loc_run:loc_run + 4] != orig_run:
                    il_data[loc_run:loc_run + 4] = orig_run

                _write_root_file(il_data, il_dest)

                subprocess_wrapper.run_as_root_and_verify(["/usr/bin/codesign", "-f", "-s", "-", str(il_dest)])
                logging.info(f"- Successfully patched and resigned {il_dest.name}")
            else:
                logging.info(f"- {il_dest.name} is already patched")

        # 2. Patch AMDMTLBronzeDriver
        if drv_dest.exists():
            logging.info(f"- In-place injecting LC_LOAD_DYLIB into {drv_dest.name}")
            with open(drv_dest, 'rb') as f:
                drv_data = bytearray(f.read())

            target_dylib = b'@loader_path/libAMDFix.dylib'
            hdr = drv_data[:32]
            magic, cputype, cpusubtype, filetype, ncmds, sizeofcmds, flags, reserved = struct.unpack('<IIIIIIII', hdr)

            if target_dylib not in drv_data[:32 + sizeofcmds]:
                path_str = target_dylib + b'\x00'
                cmdsize = 24 + len(path_str)
                pad = (8 - (cmdsize % 8)) % 8
                cmdsize += pad
                path_str += b'\x00' * pad

                lc = struct.pack('<IIIIII', 0xc, cmdsize, 24, 2, 0x10000, 0x10000) + path_str
                cmd_offset = 32 + sizeofcmds
                drv_data[cmd_offset:cmd_offset + cmdsize] = lc
                struct.pack_into('<II', drv_data, 16, ncmds + 1, sizeofcmds + cmdsize)

                _write_root_file(drv_data, drv_dest)

                subprocess_wrapper.run_as_root_and_verify(["/usr/bin/codesign", "-f", "-s", "-", str(drv_dest)])
                logging.info(f"- Successfully injected load command and resigned {drv_dest.name}")
            else:
                logging.info(f"- {drv_dest.name} already contains load command")

        # 3. Install companion helper libAMDFix.dylib
        if drv_dir.exists():
            if fix_source.exists():
                logging.info(f"- Installing {fix_dest.name} to {drv_dir}")
                subprocess_wrapper.run_as_root_and_verify(["/bin/cp", "-f", str(fix_source), str(fix_dest)])
                subprocess_wrapper.run_as_root_and_verify(["/bin/chmod", "755", str(fix_dest)])
                subprocess_wrapper.run_as_root_and_verify(["/usr/sbin/chown", "root:wheel", str(fix_dest)])
                subprocess_wrapper.run_as_root_and_verify(["/usr/bin/codesign", "-f", "-s", "-", str(fix_dest)])
            else:
                logging.error(f"- Could not find companion library source at {fix_source}")