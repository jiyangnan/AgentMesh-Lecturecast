import { interpolate, spring } from 'remotion';
import { FPS } from './theme';

// fade + slide-up reveal
export function reveal(frame: number, atSec: number, opts: { dur?: number; dy?: number } = {}) {
  const { dur = 0.5, dy = 24 } = opts;
  const at = atSec * FPS;
  const dframes = dur * FPS;
  const opacity = interpolate(frame, [at, at + dframes], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const ty = interpolate(frame, [at, at + dframes], [dy, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  return { opacity, transform: `translateY(${ty}px)` };
}

// spring pop-in
export function pop(frame: number, atSec: number, fps = FPS) {
  const at = atSec * fps;
  const s = spring({ frame: frame - at, fps, config: { damping: 12, stiffness: 180 } });
  const opacity = interpolate(frame, [at, at + 6], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  return { opacity, transform: `scale(${0.7 + 0.3 * s})` };
}

// horizontal slide
export function slideX(frame: number, atSec: number, dur = 0.5, fromX = -40) {
  const at = atSec * FPS;
  const dframes = dur * FPS;
  const opacity = interpolate(frame, [at, at + dframes], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  const tx = interpolate(frame, [at, at + dframes], [fromX, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  return { opacity, transform: `translateX(${tx}px)` };
}

// width grow (for bars / progress)
export function grow(frame: number, atSec: number, dur = 0.6) {
  const at = atSec * FPS;
  const dframes = dur * FPS;
  const w = interpolate(frame, [at, at + dframes], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
  return w;
}

// pulse highlight (subtle scale wobble after appearance)
export function pulse(frame: number, atSec: number) {
  const at = atSec * FPS;
  if (frame < at) return { transform: 'scale(1)' };
  const t = (frame - at) / FPS;
  const s = 1 + 0.04 * Math.sin(t * 6) * Math.exp(-t * 1.8);
  return { transform: `scale(${s})` };
}
