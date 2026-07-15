import React from 'react';
import {AbsoluteFill} from 'remotion';
import {layoutFor, palette} from './layout';
import {DirectorRenderProps} from './types';

export const ManifestCover: React.FC<DirectorRenderProps> = ({manifest, overrides, variant}) => {
  const layout = layoutFor(variant);
  const hook = manifest.script[0];
  const ending = manifest.script[manifest.script.length - 1];
  const title = overrides.cover?.title ?? hook?.title ?? 'LectureCast';
  const subtitle = overrides.cover?.subtitle ?? ending?.title ?? '把复杂内容讲清楚';
  const eyebrow = overrides.cover?.eyebrow ?? 'LECTURECAST · DIRECTOR';
  return (
    <AbsoluteFill style={{background: palette.background, color: palette.ink, fontFamily: 'PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif', padding: `${layout.paddingTop}px ${layout.paddingX}px`, justifyContent: 'space-between'}}>
      <div style={{fontSize: variant === 'vertical' ? 28 : 26, letterSpacing: '0.16em', fontWeight: 900, color: palette.accent}}>{eyebrow}</div>
      <div>
        <div style={{fontSize: variant === 'vertical' ? 112 : 104, fontWeight: 950, lineHeight: 1.04, letterSpacing: '-0.05em', maxWidth: variant === 'vertical' ? 1000 : 1550}}>{title}</div>
        <div style={{height: 14, width: variant === 'vertical' ? 240 : 300, background: palette.clay, margin: '36px 0'}} />
        <div style={{fontSize: variant === 'vertical' ? 48 : 42, color: palette.muted, fontWeight: 750}}>{subtitle}</div>
      </div>
      <div style={{display: 'flex', justifyContent: 'space-between', fontSize: 24, color: palette.accent, fontWeight: 800}}><span>云端导演 · 本地渲染</span><span>16:9 + 9:16</span></div>
    </AbsoluteFill>
  );
};
