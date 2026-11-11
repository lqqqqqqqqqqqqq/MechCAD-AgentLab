# Public native observation

Run inspect_native.py with the FreeCAD bundled Python on the included trusted examples. It rejects unsupported native types and executable proxies and never executes attached scripts. For untrusted inputs, also use an OS sandbox; preflight alone is not OS isolation.

The output is a geometric observation, not a full task acceptance verdict.
