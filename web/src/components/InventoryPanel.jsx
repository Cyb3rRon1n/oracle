// Equipped loadout + carried items, shared by the tabbed sheet (CharacterSheet.jsx)
// and the full-page overlay (CharacterSheetFull.jsx) - both used to keep their own
// near-identical copy of this, including a free-text "add an item" box that let a
// player type any item into existence. Removed: inventory only grows from what the
// DM narrates/grants (update_character's own add_item, DM-only), never player self-add.
// `equippable`/`usable` are server-computed (server/views.py's _item_view) so this
// component never has to know SRD categories itself - just render what it's told.

const SLOTS = ["weapon", "armor", "shield"];

export default function InventoryPanel({ sheet, edit, t }) {
  const items = sheet.inventory || [];
  const equippedName = { weapon: sheet.equipped_weapon, armor: sheet.equipped_armor, shield: sheet.equipped_shield };
  const isEquipped = (name) => SLOTS.some((slot) => equippedName[slot] === name);

  return (
    <>
      <div className="mb-3">
        <div className="text-[10px] uppercase tracking-widest text-dungeon-ink/40 mb-1">{t("Equipped")}</div>
        <div className="grid grid-cols-3 gap-1.5">
          {SLOTS.map((slot) => (
            <div key={slot} className="bg-dungeon-bg rounded border border-dungeon-edge p-1.5 text-center">
              <div className="text-[9px] uppercase tracking-widest text-dungeon-ink/40">{t(slot)}</div>
              <div className="text-xs truncate" title={equippedName[slot] || undefined}>
                {equippedName[slot] || "—"}
              </div>
              {equippedName[slot] && (
                <button
                  onClick={() => edit("unequip", equippedName[slot])}
                  className="text-[9px] text-dungeon-ink/50 hover:text-dungeon-ink underline"
                >
                  {t("unequip")}
                </button>
              )}
            </div>
          ))}
        </div>
      </div>

      <div className="text-[10px] uppercase tracking-widest text-dungeon-ink/40 mb-1">{t("Inventory")}</div>
      <ul className="space-y-1 text-sm">
        {items.length === 0 && <li className="italic text-dungeon-ink/50">{t("Empty pockets.")}</li>}
        {items.map((it) => (
          <li key={it.name + (it.magic_bonus ?? 0)} className="flex items-center justify-between gap-2">
            <span>
              {it.name}
              {it.quantity > 1 && <span className="text-dungeon-ink/50"> ×{it.quantity}</span>}
              {!!it.magic_bonus && <span className="text-dungeon-gold"> +{it.magic_bonus}</span>}
            </span>
            <span className="flex gap-1">
              {it.equippable && !isEquipped(it.name) && (
                <Mini onClick={() => edit("equip", it.name)}>{t("equip")}</Mini>
              )}
              {it.usable && <Mini onClick={() => edit("use_item", it.name)}>{t("use")}</Mini>}
              <Mini danger onClick={() => edit("remove_item", it.name)}>{t("drop")}</Mini>
            </span>
          </li>
        ))}
      </ul>
    </>
  );
}

function Mini({ children, onClick, danger }) {
  return (
    <button
      onClick={onClick}
      className={`text-[10px] px-1.5 py-0.5 rounded border transition ${
        danger
          ? "border-dungeon-blood/50 text-dungeon-blood/90 hover:bg-dungeon-blood/20"
          : "border-dungeon-edge text-dungeon-ink/60 hover:text-dungeon-ink"
      }`}
    >
      {children}
    </button>
  );
}
