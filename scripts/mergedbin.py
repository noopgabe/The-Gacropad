Import("env")

""" 
A first flash over the ROM bootloader needs the second-stage bootloader at 0x0. 
This script just makes PlatformIO merge the bootloader, partition table and app into one image at 0x0.

'Oh, but Gabriel, how do you use it?'
pio run -t mergedbin   ->  .pio/build/<env>/<env>.bin
"""

board = env.BoardConfig()

env.AddCustomTarget(
    name="mergedbin",
    dependencies=["buildprog"],
    actions=[
        '"$PYTHONEXE" "$OBJCOPY" --chip %s merge_bin '
        '-o "$BUILD_DIR/${PIOENV}.bin" '
        '--flash_mode keep --flash_size keep '
        '0x0 "$BUILD_DIR/bootloader.bin" '
        '0x8000 "$BUILD_DIR/partitions.bin" '
        '0x10000 "$BUILD_DIR/${PROGNAME}.bin"' % board.get("build.mcu")
    ],
    title="Merged Image",
    description="Merge bootloader + partitions + app into one image flashable at 0x0",
    always_build=True,
)
