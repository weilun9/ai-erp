// A dark backdrop with three slow, faint colour glows behind the cards. Pure CSS: nothing to license.
export function Background() {
  return (
    <div className="scene" aria-hidden="true">
      <div className="aura a1" />
      <div className="aura a2" />
      <div className="aura a3" />
    </div>
  );
}
