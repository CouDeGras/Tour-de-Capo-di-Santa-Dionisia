# First-use solar data

Installation only copies application files, including the compact fallback.
There are no solar-data downloads, generation steps or retry dialogs in the
installer or Electron startup.

On the first solar calculation, if `data/solar/sun-earth.bsp` is absent from
the persistent user-data folder, Django starts one daemon worker. It runs
`jplephem excerpt --targets 3,5,6,10,399` against NASA's DE421 server for ten
calendar years from that first-use date, plus two-day margins. This also works
when previewing the source through the Django development server.

The interface immediately calculates using the bundled `sun-earth-fallback.bsp`
while generation runs. If generation fails, the worker validates and copies
that fallback into the persistent location. The bundled file is 488,688 bytes,
covering 2026-11-01 through 2036-11-05. A fallback never claims a new decade.

Fresh files are atomically published only after validation. Subsequent solar
requests switch to the completed file; normal launches reuse an existing file.
A failed background preparation is logged and can be tried again after the
backend restarts. Other dashboard functions and app startup remain available.

The full ephemeris is never retained. Standard Sun/Jupiter/Saturn apparent
position corrections and exact twilight thresholds (-18, -12, -6, -50/60
degrees) are used.
