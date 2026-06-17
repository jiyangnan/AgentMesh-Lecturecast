import { AbsoluteFill, useCurrentFrame } from 'remotion';
import { COLORS as C, FONTS as F, BRAND } from './theme';
import { reveal, pop } from './anim';

// Vertical stage. Reserves bottom ~340px for burned subtitles.
export const Stage: React.FC<{ children: React.ReactNode; badge?: string; step?: string }> = ({ children, badge, step }) => {
  const frame = useCurrentFrame();
  return (
    <AbsoluteFill style={{ background: C.bg, fontFamily: F.sans, color: C.ink }}>
      {/* brand strip — edit BRAND in theme.ts, not here */}
      <div style={{ position: 'absolute', top: 70, left: 70, right: 70, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <div style={{ width: 22, height: 22, background: C.accent, borderRadius: 4 }} />
          <span style={{ fontSize: 30, fontWeight: 800, letterSpacing: 1 }}>{BRAND.series}</span>
          <span style={{ fontSize: 26, color: C.gray, fontWeight: 600 }}>· {BRAND.ep}</span>
        </div>
        {step && <span style={{ fontSize: 26, color: C.accent, fontWeight: 800, fontFamily: F.mono }}>{step}</span>}
      </div>
      {badge && (
        <div style={{ position: 'absolute', top: 150, left: 70, ...reveal(frame, 0.1, { dy: 16 }) }}>
          <span style={{ fontSize: 32, fontWeight: 800, color: C.paper, background: C.accent, padding: '8px 22px', borderRadius: 10 }}>{badge}</span>
        </div>
      )}
      <div style={{ position: 'absolute', top: badge ? 240 : 170, left: 70, right: 70, bottom: 340 }}>{children}</div>
    </AbsoluteFill>
  );
};

export const H1: React.FC<{ children: React.ReactNode; at?: number; size?: number }> = ({ children, at = 0.15, size = 72 }) => {
  const frame = useCurrentFrame();
  return (
    <div style={{ fontSize: size, fontWeight: 900, lineHeight: 1.2, letterSpacing: -1, ...reveal(frame, at, { dy: 28 }) }}>{children}</div>
  );
};

export const Accent: React.FC<{ children: React.ReactNode }> = ({ children }) => (
  <span style={{ color: C.accent }}>{children}</span>
);

// framed image card
export const Shot: React.FC<{ src: string; at: number; h?: number; crop?: 'top' | 'fit'; label?: string }> = ({ src, at, h = 760, crop = 'top', label }) => {
  const frame = useCurrentFrame();
  return (
    <div style={{ ...reveal(frame, at, { dy: 30 }) }}>
      <div style={{ height: h, overflow: 'hidden', borderRadius: 16, border: `2px solid ${C.ink}`, boxShadow: '0 12px 40px rgba(0,0,0,.12)', background: C.paper }}>
        <img src={src} style={{ width: '100%', display: 'block', objectFit: 'cover', objectPosition: crop === 'top' ? 'top' : 'center' }} />
      </div>
      {label && <div style={{ marginTop: 14, fontSize: 28, color: C.gray, fontWeight: 600 }}>{label}</div>}
    </div>
  );
};

// bullet chip that pops in
export const Chip: React.FC<{ children: React.ReactNode; at: number; color?: string }> = ({ children, at, color }) => {
  const frame = useCurrentFrame();
  return (
    <div style={{ display: 'inline-block', ...pop(frame, at) }}>
      <span style={{ fontSize: 36, fontWeight: 700, background: color || C.accentLight, color: C.ink, padding: '12px 26px', borderRadius: 12, border: `1.5px solid ${C.ink}` }}>{children}</span>
    </div>
  );
};
