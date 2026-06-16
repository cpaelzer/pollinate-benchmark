Does pollinate still earn its place on every Ubuntu boot in 2026?

Back when introduced with [14.04 Trusty Tahr](https://wiki.ubuntu.com/TrustyTahr/ReleaseNotes/14.04), [pollinate](https://blog.dustinkirkland.com/2014/02/random-seeds-in-ubuntu-1404-lts-cloud.html) helped a lot by mitigating the lack of entropy stalling boots of virtual machines and cloud guests in particular. But the world has changed.

While it clearly helped in the long past, getting entropy got easier over time and I wanted to reconsider this going forward to future releases.

## "Random Changes"

The hypothesis to test was simple: an older kernel on an older chip without virtio-rng should benefit most from pollinate, and every improvement since then should take away some of that benefit.

Which variables might influence this? (Simplified to x86 only)

Timeline of x86 hardware changes to randomness:

- 2012 Intel introduced RDRAND
- 2015 Intel introduced RDSEED and AMD introduced both

We therefore used systems from different generations and manufacturers, as you'd expect, they generally get faster and introduce rng related instructions.
The chips we tested:

- Xeon-X3430 from 2009 no rd instructions
- Xeon-E52620v3 from 2014 only RDRAND
- EPYC-7742 from 2019 with RDRAND and RDSEED
- EPYC-7502 from 2019 with RDRAND and RDSEED
- Xeon-8362 from 2021 with RDRAND and RDSEED

Newer chips would only strengthen the conclusion, so I intentionally skipped them, and competition for them in the test infrastructure was fierce anyway.

Timeline of high profile Kernel changes to randomnes:

- 2018 - v4.19 - Use chip instructions to get randomness via `CONFIG_RANDOM_TRUST_CPU`
- 2022 - v5.18 - Elimination of the Blocking Pool & Unification of /dev/random
- 2022 - v5.18 - Replacing SHA-1 with BLAKE2s for Entropy Extraction
- 2024 - v6.11 - Getrandom speedup via vDSO

Therefore I compared Ubuntu LTS Releases before and after the bigger changes:

- 2022 - Jammy with kernel v5.15
- 2026 - Resolute with kernel v7.0

And finally there is [virtio-rng](https://wiki.qemu.org/Features/VirtIORNG) through which a host can provide entropy. Entropy usually was easier to get by when physical hardware is involved, but in virtual environments it could run out.  This feature is quite old, but needed more setup than the pure availability in kernel and hardware. It was not generally in use back then, but in the meantime most common management stacks on top configure such a device by default.

- 2017 - virt-manager 1.41
- 2020 - openstack 21.0.0
- 2020 - LXD VM mode had it from day one

Despite being the default everywhere now, to understand the potential impact of virtio-rng being available or not we compared all of the above with and without virtio-rng being made available to the guest.

We'd not go all the way back to trusty more than a decade, instead we wanted to look at recent and older releases of this decade. The assumption was that an older Ubuntu release on an old chip might benefit more from pollinate, but with every improvement in the kernel and in hardware this diminishes when comparing this to a modern release on a chip from this decade.

## 3 2 1 Measure ...

The question to solve was if there is any future scenario left which suggest to keep pollinate pre-installed by default or if it became obsolete should no more be default installed and active.

These benchmark runs were not super advanced, but did the basics of:

- using a clean image
- alternating A/B tests
- disabling all kind of background load that the systems would otherwise spike on interfering with the measurements.
- Iterate a lot, detect and filter statistical outliers via the interquartile range which gladly didn't need to remove much
- track averages but also standard deviation to represent the noise
- To ensure something even needs entropy each time the necessity to generate SSH keys was re-triggered, back then that was the most common entropy sink stalling first boot

At least we can see the world got better despite popular opinion: The machine of 2009 is 4-5 times slower than the others. It has the slowest boot, the highest noise in measurements and the most consumed CPU cycles of all. In fact it was so slow that I could only take a reduced amount of measurements before the max timeout got me (which further increased relative noise).  This system skews and puts so much noise into the results that I'll leave it out for the rest of the graphs after this one. It's results were not falsifying the rest of the statements, but making them hard to be seen by not returning anything to rely on. And anyone running on chips of 2009 like me in this test is probably wasting energy by being inefficient and not too concerned about sub second boot speed, since it is orders of magnitude higher than anything more recent :-) This can also be seen on consumed much cpu time it needs to run pollinate (which is another metric I gathered), all the others are in 0.13-0.2s and this one is at about 0.45s of a (much slower) cpu spent.

TODO - Graph 1 ONIBI vs the other CPUs in default config Speed

Off the RNG topic while speaking about things getting better: I'm happy to see that we made resolute userspace boot time faster when comparing to jammy, no matter which hardware combination I look at.

TODO - Graph 2 resolute % of jammy boot time across CPUS in default config (no ONIBI)

Furthermore the comparison with and without virtio-rng turned out uninteresting: no effect worth a graph.

With the noisy outlier set aside, the real question — does pollinate still help?
Let us look at what we found to get a clear answer.

When we look at the 22.04 jammy results we can see there wasn't too much difference.  Pollinate was still minimally helpful sometimes, but already started to show diminishing returns with the results effectively being well within the noise range.

TODO - Graph 3 jammy only across all CPUs except ONIBI - pollinate does not change things

But when we look at the more recent resolute the expected pattern starts to show.  Pollinate now actually more likely slows down boot speed. And while doing so it is also spending some cpu cycles, not much but any effort is wasted if not contributing.

TODO - Graph resolute only across all CPUs except ONIBI - pollinate makes it worse
TODO -   second axis cpu consumption in s with 0.x being the scale

## Conclusion

My hunch that it was about time to drop this turned out right. I was able to prove that it became less useful over time until in very recent releases especially on more modern hardware it starts to become counterproductive. In such modern setups it slows boot time by about a third of a second, while spending roughly a fifth of a second of a CPU to do so.

Usually I call you to action at this stage of a post. But this one is not about any single admin tuning their box - it is a decision for all of us in Ubuntu.  Pollinate ships on every Ubuntu by default, which means every boot on modern hardware pays a small tax for something that no longer helps. Removing it is the kind of change that quietly benefits the whole ecosystem at once.

Due to that I'd say:
    "to speed you up we remove what sped you up!"

And if you miss the extra entropy, I have some alternative source of entropy from my manager-makes-decisions set:

TODO - Image of my management dices

P.S. You want it to be more aggressive: I wish I'd have found the time for this earlier, I'm tending to discuss to remove it even from resolutes default images.  Such is not meant to happen after release, but the data gathered suggests that already for resolute it isn't having a positive contribution anymore.

P.P.S. You want it to be softer: This does not mean you can't use pollinate. If you have very old hardware and a special use case that has none of the helpful features - no problem, it was not removed from the archive - you can add it to your custom images if you like.
