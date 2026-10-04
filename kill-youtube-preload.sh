#!/bin/sh
# YouTube ships with supportQuickStart:true too (same mechanism as Netflix,
# see kill-netflix.sh), preloading its renderer in the background at every
# boot regardless of the TV's own Quick Start+ setting (confirmed live:
# that setting does NOT stop this). Unlike Netflix, the killed renderer
# gets respawned by the platform within a few seconds -- but the fresh
# respawn starts cold (minimal memory footprint) instead of the fully
# warmed-up preloaded instance, so killing it right before launch still
# meaningfully frees RAM for the stream that's about to start (confirmed
# live: freed ~240MB of available memory in one test).
#
# com.webos.app.lgchannels has the same preload problem but its renderer
# was found stuck in kernel state D (disk sleep) via webOS's own cgroup
# freezer (__refrigerator in its kernel stack) -- deliberately frozen to
# save CPU while backgrounded, not hung, but this also makes it immune to
# every signal including SIGKILL until thawed. Not handled here; a reboot
# is the only thing confirmed to clear it.
YTB="youtube.leanback.v4"
pkill -f "app-id=$YTB"
exit 0
