// Portrait avatar + generate/regenerate control, shared by the full sheet
// overlay and the tabbed sheet's Overview tab. No portrait yet -> initials
// on a deterministic color swatch (hashed from the name, no lookup table
// needed) - same fallback idea as nightwire's own portraitFor(), simplified
// since oracle has no role enum to key a real palette off of.

export default function Avatar({ sheet, pending, onGenerate, t }) {
  const name = sheet.name || "?";
  const initials = name
    .split(/\s+/)
    .map((w) => w[0])
    .filter(Boolean)
    .slice(0, 2)
    .join("")
    .toUpperCase();
  const hue = [...name].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 0);

  return (
    <div className="flex flex-col items-center gap-1 shrink-0">
      <div
        className="w-14 h-14 rounded-full border border-dungeon-edge overflow-hidden flex items-center justify-center"
        style={sheet.portrait ? undefined : { backgroundColor: `hsl(${hue}, 35%, 25%)` }}
      >
        {sheet.portrait ? (
          <img src={sheet.portrait} alt="" className="w-full h-full object-cover" />
        ) : (
          <span className="text-sm font-display text-dungeon-ink/80">{initials}</span>
        )}
      </div>
      <button
        onClick={onGenerate}
        disabled={pending}
        className="text-[9px] text-dungeon-ink/50 hover:text-dungeon-ink underline disabled:opacity-40 disabled:pointer-events-none disabled:no-underline"
      >
        {pending ? t("Generating…") : sheet.portrait ? t("Regenerate") : t("Generate portrait")}
      </button>
    </div>
  );
}
