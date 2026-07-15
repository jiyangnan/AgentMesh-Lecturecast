import React from 'react';
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {layoutFor, palette} from '../layout';

export const SceneFrame: React.FC<React.PropsWithChildren<{
  variant: 'vertical' | 'landscape';
  eyebrow?: string;
}>> = ({variant, eyebrow, children}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const layout = layoutFor(variant);
  const enter = spring({frame, fps, config: {damping: 18, stiffness: 120}});
  return (
    <AbsoluteFill style={{
      background: palette.background,
      color: palette.ink,
      fontFamily: 'PingFang SC, Hiragino Sans GB, Microsoft YaHei, sans-serif',
      padding: `${layout.paddingTop}px ${layout.paddingX}px ${layout.paddingBottom}px`,
      opacity: interpolate(frame, [0, 8], [0, 1], {extrapolateRight: 'clamp'}),
    }}>
      {eyebrow ? (
        <div style={{fontSize: variant === 'vertical' ? 26 : 24, letterSpacing: '0.14em', fontWeight: 800, color: palette.accent, marginBottom: 34}}>
          {eyebrow}
        </div>
      ) : null}
      <div style={{flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', transform: `translateY(${(1 - enter) * 36}px)`}}>
        {children}
      </div>
    </AbsoluteFill>
  );
};

export const Title: React.FC<{variant: 'vertical' | 'landscape'; children: React.ReactNode}> = ({variant, children}) => {
  const layout = layoutFor(variant);
  return <div style={{fontSize: layout.titleSize, fontWeight: 900, lineHeight: 1.12, letterSpacing: '-0.035em'}}>{children}</div>;
};

export const Card: React.FC<React.PropsWithChildren<{tone?: 'paper' | 'sage' | 'clay'; style?: React.CSSProperties}>> = ({tone = 'paper', style, children}) => {
  const backgrounds = {paper: palette.paper, sage: '#DCE3DE', clay: '#EAD8CB'};
  return (
    <div style={{background: backgrounds[tone], border: `2px solid ${palette.sage}`, borderRadius: 28, padding: 34, boxShadow: '0 18px 48px rgba(41,51,46,0.08)', ...style}}>
      {children}
    </div>
  );
};

export const asString = (value: unknown, fallback = ''): string => typeof value === 'string' ? value : fallback;
export const asStrings = (value: unknown): string[] => Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];

