// A drawn sky-and-meadow scene (CSS gradients + SVG hills): no photos, nothing to license.
export function Background() {
  return (
    <div className="scene" aria-hidden="true">
      <div className="sky" />
      <div className="sun" />
      <div className="cloud c1" />
      <div className="cloud c2" />
      <div className="cloud c3" />
      <svg className="hills" viewBox="0 0 1440 520" preserveAspectRatio="none">
        <defs>
          <linearGradient id="far" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#b9d98f" />
            <stop offset="1" stopColor="#8fbf6a" />
          </linearGradient>
          <linearGradient id="mid" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#8cc45f" />
            <stop offset="1" stopColor="#5f9e3f" />
          </linearGradient>
          <linearGradient id="near" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0" stopColor="#6fb247" />
            <stop offset="1" stopColor="#3f7d2c" />
          </linearGradient>
        </defs>
        <path fill="url(#far)" opacity="0.85"
          d="M0 210 C 180 150 330 170 520 190 C 720 212 860 140 1060 150 C 1230 158 1340 196 1440 182 L1440 520 L0 520Z" />
        <path fill="url(#mid)"
          d="M0 300 C 210 238 380 262 600 290 C 820 318 980 236 1190 246 C 1310 252 1390 280 1440 274 L1440 520 L0 520Z" />
        <path fill="url(#near)"
          d="M0 400 C 260 340 470 372 720 392 C 980 412 1160 350 1440 368 L1440 520 L0 520Z" />
      </svg>
      <div className="haze" />
    </div>
  );
}
