import { AbsoluteFill, Audio, Sequence, staticFile } from 'remotion';
import { COLORS, FPS, FONTS, SECTIONS } from './theme';
import { Hook } from './scenes/Hook';
import { End } from './scenes/End';
// 👉 每期：给每个 section 写一个 scenes/<Id>.tsx，import 进来并加进 SCENES。
//    key = scripts/bilibili.json 里 section id 去掉 "NN_" 前缀（01_hook → hook）。
//    Hook + End 是随模板附带的 working example，照着改文案/布局即可。

const SCENES: Record<string, React.FC> = {
  hook: Hook,
  end: End,
  // why: Why, collect: Collect, analyze: Analyze, ... ← 每期补齐
};

export const Video: React.FC = () => {
  return (
    <AbsoluteFill style={{ background: COLORS.bg, fontFamily: FONTS.sans, color: COLORS.ink }}>
      <Audio src={staticFile('narration.mp3')} />
      {SECTIONS.map(({ id, start, duration }) => {
        const Comp = SCENES[id];
        if (!Comp) return null;
        return (
          <Sequence key={id} from={Math.round(start * FPS)} durationInFrames={Math.round(duration * FPS)} name={id}>
            <Comp />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};
