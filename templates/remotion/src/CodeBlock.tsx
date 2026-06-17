import React from 'react';
import { interpolate, useCurrentFrame } from 'remotion';
import { COLORS, FONTS, FPS } from './theme';

// Token type → color
type Tok = 'k' | 's' | 'n' | 'v' | 'd' | 'c' | 'p';
const TOKEN_COLORS: Record<Tok, string> = {
  k: COLORS.codePink,   // keyword: from import def if return
  s: COLORS.codeGreen,  // string
  n: COLORS.codeOrange, // function/class name
  v: COLORS.codeBlue,   // variable/type
  d: COLORS.codeYellow, // decorator
  c: COLORS.codeGray,   // comment
  p: COLORS.codeFg,     // plain
};

export type CodeLine = {
  // Per-line reveal time in seconds (local to the scene)
  at: number;
  // Tokenized content. Each tuple is [type, text].
  tokens: [Tok, string][];
  // Optional indent prefix in spaces
  indent?: number;
};

type Props = {
  filename?: string;
  lines: CodeLine[];
  fontSize?: number;
  width?: number | string;
  height?: number | string;
};

export const CodeBlock: React.FC<Props> = ({ filename, lines, fontSize = 30, width = '100%', height = '100%' }) => {
  const f = useCurrentFrame();
  return (
    <div style={{ width, height, display: 'flex', flexDirection: 'column', borderRadius: 14, overflow: 'hidden', boxShadow: '0 8px 0 0 rgba(0,0,0,0.85)' }}>
      {filename && (
        <div style={{ background: '#11111B', color: COLORS.codeYellow, padding: '14px 24px', fontFamily: FONTS.mono, fontSize: fontSize * 0.7, fontWeight: 600, letterSpacing: '0.05em', display: 'flex', alignItems: 'center', gap: 12 }}>
          <span>📄</span>
          <span>{filename}</span>
        </div>
      )}
      <div style={{ flex: 1, background: COLORS.codeBg, color: COLORS.codeFg, fontFamily: FONTS.mono, fontSize, lineHeight: 1.55, padding: '28px 28px 28px 0', display: 'flex' }}>
        {/* gutter */}
        <div style={{ width: fontSize * 2.4, textAlign: 'right', color: COLORS.codeGray, paddingRight: 18, paddingLeft: 18, fontSize: fontSize * 0.78, lineHeight: (fontSize * 1.55) / (fontSize * 0.78) }}>
          {lines.map((_, i) => <div key={i}>{i + 1}</div>)}
        </div>
        {/* lines */}
        <div style={{ flex: 1 }}>
          {lines.map((line, i) => {
            const atFr = line.at * FPS;
            const opacity = interpolate(f, [atFr, atFr + 10], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
            const ty = interpolate(f, [atFr, atFr + 10], [6, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
            return (
              <div key={i} style={{ opacity, transform: `translateY(${ty}px)`, minHeight: '1.55em' }}>
                {' '.repeat(line.indent || 0)}
                {line.tokens.map(([t, text], j) => (
                  <span key={j} style={{ color: TOKEN_COLORS[t] }}>{text}</span>
                ))}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
};
