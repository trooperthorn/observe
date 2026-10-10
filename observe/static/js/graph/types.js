// Ported from trooperthorn/relationship-maps, packages/graph-core (commit 0c4d268), via ha_Int_soc (MIT). No d3.
// Shapes used by the graph engine, as JSDoc typedefs only. The arc layout shapes are not ported.

/**
 * @typedef {Object} Category
 * @property {string} id
 * @property {string} label
 */

/**
 * @typedef {Object} GraphEntity
 * @property {string} id
 * @property {string} name
 * @property {string} group    Category id. Decides the colour, taken from the --cat-* tokens.
 * @property {string} [kind]   Free-form sub-type shown in the side card.
 * @property {string} [state]  up, warn, serious, down, pending or unreach. Drawn as a ring and a glyph.
 * @property {string} [badge]  Short text beside the label: a state word or a count.
 * @property {string} [sub]    Second line of the label: the state word and the device type.
 */

/**
 * @typedef {Object} GraphRelation
 * @property {string} id
 * @property {string} source
 * @property {string} target
 * @property {string} kind     uplink, lldp or mac.
 * @property {boolean} [stale] Drawn dashed when the link has not been confirmed for a while.
 */

/**
 * @typedef {Object} NodePos
 * @property {string} entityId
 * @property {number} x
 * @property {number} y
 * @property {number} r
 * @property {number} group    Index of the group, mapped to a --cat-* token at paint time.
 * @property {string} state
 * @property {boolean} anchor
 */

/**
 * @typedef {Object} LinkPos
 * @property {number} x1
 * @property {number} y1
 * @property {number} x2
 * @property {number} y2
 * @property {string} source
 * @property {string} target
 * @property {string} kind
 * @property {boolean} stale
 */

/**
 * @typedef {Object} Layout
 * @property {NodePos[]} nodes
 * @property {LinkPos[]} links
 * @property {boolean} limited  True when the graph is over the node limit and no layout was made.
 * @property {number} ticks     How many simulation steps ran.
 */

export {};
