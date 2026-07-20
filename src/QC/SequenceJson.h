// SequenceJson.h — JSON (de)serialization for PebblingSequence: the file
// interface between David's SAT-based pebbling solver and PebblingMethod.
//
// Schema:
//   { "num_ancillas": 4,
//     "steps": [ {"action":"pebble",  "node":12, "anc":0},
//                {"action":"unpebble","node":12, "anc":0} ] }
//
// "num_ancillas" is optional on read and cross-checked against the value
// derived from the steps (sequenceNumAncillas) — a mismatch is an error.
// Node indices are mockturtle node indices of the SAME xag_network the
// translator will consume (ctx.xag). Because random-XAG generation is not
// deterministic across platforms/stdlibs, pair sequence files with the XAG
// structure dump (xagStructureToJSON) rather than only (pis,ands,xors,seed).

#ifndef SEQUENCEJSON_H
#define SEQUENCEJSON_H

#include "PebblingSequence.h"
#include <mockturtle/networks/xag.hpp>
#include <string>

namespace xagtdep {

/// Serialize a sequence to the JSON schema above.
std::string sequenceToJSON(const PebblingSequence &seq);

/// Parse a sequence from JSON. Input is untrusted: any shape violation
/// (missing/mistyped fields, unknown action, negative numbers, num_ancillas
/// mismatch) throws PebblingError. XAG-dependent checks (node bounds,
/// gate-ness) happen in PebblingMethod before any gate is emitted.
PebblingSequence sequenceFromJSON(const std::string &json_str);

/// File convenience wrappers. Reading throws PebblingError on an unreadable
/// file or malformed content; writing returns false on I/O failure.
bool writeSequenceFile(const std::string &path, const PebblingSequence &seq);
PebblingSequence readSequenceFile(const std::string &path);

/// Dump the XAG structure (PIs, POs, gates with fanins/complements) so the
/// solver side sees the exact graph and node indices the translator will use.
std::string xagStructureToJSON(const mockturtle::xag_network &xag);

} // namespace xagtdep

#endif // SEQUENCEJSON_H
