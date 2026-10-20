# CPU frequency control and CPU thermal throttling (kernel side)

How the project kernel drives CPU frequency and throttles on temperature. Confidence labels as elsewhere:
*confirmed* = read in code, DT or observed; *strong* = source reading; *inferred* = reasoning.

## What is enabled

- `ARM_QCOM_CPUFREQ_HW=y` with `schedutil` as the default governor, `CPU_FREQ_THERMAL=y`,
  `THERMAL_DEFAULT_GOV_STEP_WISE=y`, energy model on. `ARM64_AMU_EXTN` is off.
- The DTS enables `&cpufreq_hw` and **deletes `operating-points-v2` from all eight CPU nodes**, so the driver
  builds its operating-point table from the hardware lookup table only (the EPSS block at
  0x18591000 / 0x18592000 / 0x18593000, the same pages the downstream kernel maps).
- CPU and cluster thermal zones are polled every 100 ms, the downstream values.
- `QCOM_LMH` is off and `epss_l3` stays disabled.

## Design notes

**EPSS register access touches only the apps performance-state pages.** The driver writes `PERF_STATE`
(0x320) on a frequency change, the per-core copies only if the control bit says so, and `INTR_CLR`
(0x308) in the dcvsh interrupt handler, the same registers downstream writes. It makes no secure call.
The `xo` and `alternate` clocks are only looked up and read. (confirmed)

**No AMU.** With `ARM64_AMU_EXTN=n` no activity-monitor registers are read; frequency invariance falls back
to the cpufreq software scale. (confirmed)

**No kernel-side voltages.** No CPU node has a `cpu-supply`; the lookup-table voltages only feed the energy
model. The hardware applies the rail voltage per performance state from its own table, as under Android
(inferred). The two LMH reference supplies the downstream driver votes for (0.88 V and 1.8 V) are
`regulator-always-on` in the DTS.

**No interconnect or L3 votes.** With a DT OPP table the driver would vote DDR bandwidth and write the L3
performance state on each change, and would need `epss_l3`. Deleting the OPP tables avoids that. The cost is
that L3 and CPU-to-DDR bandwidth do not follow CPU frequency; they keep whatever Android last set, so
memory-bound code may be L3-limited (inferred).

**Frequencies** are `xo_rate x L_VAL` rows read from the lookup table. Expected tables (same as downstream):

| Policy | CPUs (MPIDR) | Min / max kHz | Steps |
|---|---|---|---|
| policy0 (silver) | 0-3 (0x000-0x300) | 300000 / 1804800 | 17 |
| policy4 (gold) | 4-6 (0x400-0x600) | 710400 / 2419200 | 18 |
| policy7 (prime) | 7 (0x700) | 844800 / 2841600 | 20 |

(Policy numbers assume the boot CPU is MPIDR 0; check `related_cpus`.) The starting frequency is whatever
the hardware was running when the kernel started; if it is not a table entry the core moves to the next
table entry and logs `Running at unlisted initial frequency`.

**LMh (hardware limits management) is not configured by Linux.** Mainline `qcom-lmh` has no SM8250 support,
and downstream never configured it from Linux either (`qcom,limits-dcvs` is referenced by no CPU). The
hardware runs as the firmware left it. The kernel keeps the dcvsh interrupts: when hardware throttles, the
driver polls the domain state and reports hardware pressure to the scheduler, and `scaling_cur_freq` shows the
hardware-limited frequency.

**Governor and caps.** schedutil moves each policy to its maximum under sustained load. There is no
kernel-side cap; the maximum comes from the table. A cap is a runtime `scaling_max_freq` write. The stock
90 C limit zones clamp gold to 1478400 kHz and prime to 1075200 kHz (silver is not clamped).

## CPU thermal zones (step_wise)

Cooling devices are `cpufreq-cpu0`, `cpufreq-cpu4`, `cpufreq-cpu7` (max state = table entries - 1) and the fan.

| Zone | Trips (temp / hysteresis) | Cooling |
|---|---|---|
| gold and prime CPU zones | 70 C / 10 active; 90 C / 5 passive; 95 C / 2 passive; 110 C critical | 90 C: cpufreq clamp (gold 0..10, prime 0..17, the downstream values); 95 C: gold and prime cpufreq with no limit; 70 C: fan |
| silver CPU zones | 70 C active; 90 C and 95 C passive; 110 C critical | fan, and the dtsi's silver maps |
| cluster zones | 70 C active; 90 C hot; 110 C critical | fan only |

Gold state 10 is 1478400 kHz and prime state 17 is 1075200 kHz (assuming the downstream lists). Two downstream
mechanisms are not carried over: the 75 C "notify" trip (it only toggled a skip row in the downstream table,
and this table has none) and the 110 C core-isolate trip (the 110 C critical trip, an orderly shutdown,
replaces it).

## Observed

On the device: cpufreq works (a `scaling_max_freq` cap can be applied and lifted at runtime), the fan runs
under direct control (about 8.7 krpm at full duty), and an 8-core load held for several minutes peaked at
about 55-56 C.
