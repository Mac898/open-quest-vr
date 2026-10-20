# IMU axis frame

The IMU lives behind the syncboss sensor hub (see [sensors-connectivity.md](sensors-connectivity.md)). It
reports at about **987.5 Hz** (the firmware default). Accelerometer values are in g, gyroscope values in
degrees per second.

## Axes (confirmed: gyro captures and static gravity poses agree)

| IMU axis | Headset direction (as worn) | Evidence |
|---|---|---|
| **+X** | **up** (top of the headset) | level pose reads +1.00 g on X, upside down -1.00; yaw rotation shows up mainly on X |
| **+Y** | **right** | left-side-down reads +0.98 g on Y, right-side-down -0.99; pitch rotation shows up mainly on Y |
| **+Z** | **forward** (out of the visor, away from the face) | visor-up reads +0.99 g on Z, visor-down -1.00; roll rotation shows up mainly on Z; lying lenses-up on a table reads -Z |

The frame is right-handed (X x Y = Z). The accelerometer reports specific force: +1 g on the axis pointing up
at rest. At rest the acceleration norm is about 0.9995 g and the gyro norm about 0.7 deg/s (uncalibrated bias).
One unit's gyro bias was about (+0.52, -0.45, +0.13) deg/s.

## Conversion to OpenXR head space (x right, y up, z back)

```
x_xr = +Y_imu
y_xr = +X_imu
z_xr = -Z_imu
```

Apply it to both accelerometer and gyroscope; convert g to m/s^2 (x 9.80665) and deg/s to rad/s (x pi/180).
Mounting offset and per-unit calibration (biases, misalignment, IMU-to-display extrinsics) are not known;
per-unit calibration is stored on the device.

## Open

- Small cross-axis terms in the poses (about 0.12 g) are most likely hand-held pose error; a fixture capture
  would separate them from IMU misalignment.
- The sign of each gyro axis against rotation direction follows from the right-handed frame; it has not been
  checked against a scripted rotation direction.
