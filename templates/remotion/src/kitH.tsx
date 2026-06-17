import { AbsoluteFill, useCurrentFrame } from 'remotion';
import { COLORS as C, FONTS as F, BRAND } from './theme';
import { reveal } from './anim';

// Landscape 1920x1080 stage. Reserves bottom ~150px for burned subtitles.
export const StageH: React.FC<{ children: React.ReactNode; badge?: string; step?: string }> = ({ children, badge, step }) => {
  return (
    <AbsoluteFill style={{ background: C.bg, fontFamily: F.sans, color: C.ink }}>
      <div style={{ position: 'absolute', top: 48, left: 80, right: 80, display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
          <div style={{ width: 20, height: 20, background: C.accent, borderRadius: 4 }} />
          <span style={{ fontSize: 30, fontWeight: 800, letterSpacing: 1 }}>{BRAND.series}</span>
          <span style={{ fontSize: 25, color: C.gray, fontWeight: 600 }}>· {BRAND.ep}</span>
          {badge && <span style={{ fontSize: 26, fontWeight: 800, color: C.paper, background: C.accent, padding: '6px 18px', borderRadius: 8, marginLeft: 10 }}>{badge}</span>}
        </div>
        {step && <span style={{ fontSize: 25, color: C.accent, fontWeight: 800, fontFamily: F.mono }}>{step}</span>}
      </div>
      <div style={{ position: 'absolute', top: 120, left: 80, right: 80, bottom: 150 }}>{children}</div>
    </AbsoluteFill>
  );
};

export const TitleH: React.FC<{ children: React.ReactNode; at?: number; size?: number }> = ({ children, at = 0.15, size = 66 }) => {
  const frame = useCurrentFrame();
  return <div style={{ fontSize: size, fontWeight: 900, lineHeight: 1.18, letterSpacing: -1, ...reveal(frame, at, { dy: 24 }) }}>{children}</div>;
};

export const AccentH: React.FC<{ children: React.ReactNode }> = ({ children }) => <span style={{ color: C.accent }}>{children}</span>;

// framed image (landscape): fits inside a box, top-cropped
export const ShotH: React.FC<{ src: string; at: number; w?: number; h?: number; crop?: 'top' | 'fit'; label?: string }> = ({ src, at, w = 900, h = 620, crop = 'top', label }) => {
  const frame = useCurrentFrame();
  return (
    <div style={{ ...reveal(frame, at, { dy: 26 }) }}>
      <div style={{ width: w, height: h, overflow: 'hidden', borderRadius: 16, border: `2px solid ${C.ink}`, boxShadow: '0 12px 40px rgba(0,0,0,.12)', background: C.paper }}>
        <img src={src} style={{ width: '100%', display: 'block', objectFit: 'cover', objectPosition: crop === 'top' ? 'top' : 'center' }} />
      </div>
      {label && <div style={{ marginTop: 12, fontSize: 26, color: C.gray, fontWeight: 600 }}>{label}</div>}
    </div>
  );
};
