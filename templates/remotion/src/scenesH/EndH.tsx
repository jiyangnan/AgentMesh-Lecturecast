import { AbsoluteFill, useCurrentFrame } from 'remotion';
import { COLORS as C, FONTS as F, BRAND } from '../theme';
import { reveal, pop, pulse } from '../anim';

export const EndH: React.FC = () => {
  const frame = useCurrentFrame();
  const polls = ['AI 自动剪视频', 'AI 做小红书图文', 'AI 做数据采集', '你来定 →'];
  return (
    <AbsoluteFill style={{ background: C.ink, fontFamily: F.sans, color: C.paper }}>
      <div style={{ position: 'absolute', top: 70, left: 0, right: 0, textAlign: 'center', ...reveal(frame, 0.1) }}>
        <span style={{ fontSize: 32, fontWeight: 800, color: C.accent, letterSpacing: 2 }}>{BRAND.series} · {BRAND.ep}</span>
      </div>
      <div style={{ position: 'absolute', top: 200, left: 0, right: 0, textAlign: 'center', ...pop(frame, 0.8) }}>
        <div style={{ display: 'inline-block', background: C.accent, borderRadius: 24, padding: '44px 80px', boxShadow: '0 20px 60px rgba(255,92,0,.35)' }}>
          <div style={{ fontSize: 40, fontWeight: 700, opacity: .95 }}>把一个爆款</div>
          <div style={{ fontSize: 80, fontWeight: 900, marginTop: 14, ...pulse(frame, 2) }}>逆向成「<span style={{ color: C.ink }}>方法</span>」</div>
          <div style={{ fontSize: 40, fontWeight: 700, marginTop: 16 }}>这个视角，谁都能用</div>
        </div>
        <div style={{ fontSize: 28, color: '#bbb', marginTop: 20, ...reveal(frame, 3) }}>工具已开源 · 学方法，不抄搬运</div>
      </div>
      <div style={{ position: 'absolute', top: 700, left: 0, right: 0, textAlign: 'center', ...reveal(frame, 5, { dy: 22 }) }}>
        <div style={{ fontSize: 38, fontWeight: 800, marginBottom: 22 }}>下期实战，你想看哪个？</div>
        <div style={{ display: 'flex', gap: 18, justifyContent: 'center' }}>
          {polls.map((p, i) => (
            <span key={i} style={{ fontSize: 32, fontWeight: 700, border: `2px solid ${C.accent}`, borderRadius: 14, padding: '14px 28px', ...pop(frame, 5.5 + i * 0.4) }}>{p}</span>
          ))}
        </div>
      </div>
    </AbsoluteFill>
  );
};
