#!/bin/sh
# Netflix ships with supportQuickStart:true baked into its own manifest, so
# it preloads in the background regardless of the TV's own Quick Start+
# setting -- confirmed via a live crash where free RAM hit 42MB right as a
# video stream switch needed a large allocation, with Netflix sitting on
# ~90MB in the background the whole time. Killing it here is a one-shot,
# fire-and-forget action (unlike YouTube, confirmed live to stay dead
# rather than being respawned by the platform).
pkill -f netflix.bin
exit 0
