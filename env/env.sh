# Env for OAI-math RL environment on the pod.
# usage: source ~/rl/env/env.sh
export HOME_REAL=$HOME
export PATH=$HOME/rl/bin:$HOME/rl/leanbin:$HOME/rl/landrun:$HOME/rl/lean4export/.lake/build/bin:$HOME/rl/comparator/.lake/build/bin:$HOME/.elan/bin:$PATH
export OAI_LEAN_ROOT=$HOME/rl/math/lean
export OAI_EP_ROOT=$HOME/rl/episodes
# comparator deps
export COMPARATOR_LANDRUN=$HOME/rl/bin/landrun
export COMPARATOR_LEAN4EXPORT=$HOME/rl/lean4export/.lake/build/bin/lean4export
