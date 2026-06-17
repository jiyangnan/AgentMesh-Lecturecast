import { AbsoluteFill, useCurrentFrame } from 'remotion';
import { COLORS as C, FONTS as F, BRAND } from '../theme';
import { reveal, pop, pulse } from '../anim';

export const HookH: React.FC = () => {
  const frame = useCurrentFrame();
  const qs = [['怎么选题？', 200, 240], ['怎么起标题？', 1480, 300], ['怎么收金句？', 250, 760]];
  return (
    <AbsoluteFill style={{ background: C.bg, fontFamily: F.sans, color: C.ink }}>
      <div style={{ position: 'absolute', top: 70, left: 0, right: 0, textAlign: 'center', ...reveal(frame, 0.1) }}>
        <span style={{ fontSize: 34, fontWeight: 800, color: C.accent, letterSpacing: 3 }}>{BRAND.series} · {BRAND.ep}</span>
      </div>
      {qs.map(([q, x, y], i) => (
        <div key={i} style={{ position: 'absolute', left: x as number, top: y as number, ...pop(frame, 3 + i * 0.6) }}>
          <span style={{ fontSize: 40, fontWeight: 700, background: C.paper, border: `2px solid ${C.ink}`, borderRadius: 16, padding: '16px 30px', boxShadow: '0 8px 24px rgba(0,0,0,.08)' }}>{q}</span>
        </div>
      ))}
      <div style={{ position: 'absolute', top: 380, left: 0, right: 0, textAlign: 'center' }}>
        <div style={{ ...reveal(frame, 0.4, { dy: 34 }) }}>
          <span style={{ fontSize: 130, fontWeight: 900, letterSpacing: -2 }}>10 分钟 </span>
          <span style={{ fontSize: 110, fontWeight: 900, ...pulse(frame, 1.2) }}>拆解<span style={{ color: C.accent }}>任何爆款博主</span></span>
        </div>
        <div style={{ marginTop: 40, fontSize: 44, fontWeight: 600, color: C.ink2, ...reveal(frame, 5.5) }}>丢一个链接进去 · 手把手 · 还开源</div>
        <div style={{ margin: '46px auto 0', width: 240, height: 12, background: C.accent, borderRadius: 6, ...reveal(frame, 0.5) }} />
      </div>
    </AbsoluteFill>
  );
};
