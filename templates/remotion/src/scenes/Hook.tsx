import { AbsoluteFill, useCurrentFrame } from 'remotion';
import { COLORS as C, FONTS as F, BRAND } from '../theme';
import { reveal, pop, pulse } from '../anim';

export const Hook: React.FC = () => {
  const frame = useCurrentFrame();
  const qs = ['怎么选题？', '怎么起标题？', '怎么收金句？'];
  return (
    <AbsoluteFill style={{ background: C.bg, fontFamily: F.sans, color: C.ink }}>
      {/* brand */}
      <div style={{ position: 'absolute', top: 90, left: 0, right: 0, textAlign: 'center', ...reveal(frame, 0.1) }}>
        <span style={{ fontSize: 34, fontWeight: 800, color: C.accent, letterSpacing: 2 }}>{BRAND.series} · {BRAND.ep}</span>
      </div>

      {/* floating questions */}
      <div style={{ position: 'absolute', top: 360, left: 70, right: 70, display: 'flex', flexDirection: 'column', gap: 28 }}>
        {qs.map((q, i) => (
          <div key={i} style={{ alignSelf: i % 2 ? 'flex-end' : 'flex-start', ...pop(frame, 3.0 + i * 0.7) }}>
            <span style={{ fontSize: 46, fontWeight: 700, background: C.paper, border: `2px solid ${C.ink}`, borderRadius: 18, padding: '18px 34px', boxShadow: '0 8px 24px rgba(0,0,0,.08)' }}>{q}</span>
          </div>
        ))}
      </div>

      {/* big title */}
      <div style={{ position: 'absolute', top: 760, left: 70, right: 70, textAlign: 'center' }}>
        <div style={{ ...reveal(frame, 0.4, { dy: 40 }) }}>
          <div style={{ fontSize: 120, fontWeight: 900, lineHeight: 1.05, letterSpacing: -2 }}>
            10 分钟
          </div>
          <div style={{ fontSize: 96, fontWeight: 900, lineHeight: 1.1, marginTop: 8, ...pulse(frame, 1.2) }}>
            拆解<span style={{ color: C.accent }}>任何爆款博主</span>
          </div>
        </div>
        <div style={{ marginTop: 50, fontSize: 44, fontWeight: 600, color: C.ink2, ...reveal(frame, 6.0) }}>
          丢一个链接进去 · 手把手 · 还开源
        </div>
      </div>

      {/* accent underline */}
      <div style={{ position: 'absolute', top: 1240, left: '50%', width: 220, height: 12, background: C.accent, borderRadius: 6, transform: 'translateX(-50%)', ...reveal(frame, 0.5) }} />
    </AbsoluteFill>
  );
};
