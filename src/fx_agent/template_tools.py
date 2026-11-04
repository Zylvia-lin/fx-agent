"""MCP entry points for static source studies and existing memory admission."""

import asyncio
import threading
from contextlib import suppress

from fx_agent.archive import ArchiveCancelled, ArtifactInput
from fx_agent.effect_cards import EffectCardInput, EffectExclusion, EffectLibrary, MediaReview
from fx_agent.effect_study import EffectStudy
from fx_agent.store import compact
from fx_agent.templates import TemplateLibrary, TemplatePolicy


def register_template_tools(mcp, root_provider):
    @mcp.tool()
    async def fx_plan_effect_study(
        study_id: str,
        media_source: str = "",
        section: str = "all",
        offset: int = 0,
        limit: int = 50,
        max_chars: int = 12000,
        node_path: str = "",
    ) -> dict:
        """Trace author labels and render outputs, inventory supplied movies/sequences.

        Re-extracts the archived original without changing old history. Supply the
        user-provided media directory explicitly; never scans a live GUI scene.
        Paginate annotations/render_outputs/media. Inspect sample_paths, the whole
        movie and dense temporal samples before naming effects. All items start
        not_viewed; path matches and spatial box candidates are not visual proof.
        Unclear GEO: follow related_node_paths to outputs, inspect their sequences,
        then ask the user only about remaining missing/ambiguous evidence.
        """
        return await asyncio.to_thread(
            EffectStudy(root_provider()).plan,
            study_id,
            media_source,
            section=section,
            offset=offset,
            limit=limit,
            max_chars=max_chars,
            node_path=node_path,
        )

    @mcp.tool()
    async def fx_get_effect_learning_queue(
        offset: int = 0,
        limit: int = 20,
        max_chars: int = 12000,
    ) -> dict:
        """List current studies needing second-pass identification or user answers.

        Old cards remain searchable. Revise using all current supersedes, preserve
        effect_key, and record new evidence/names. Never invent missing visuals.
        """
        return await asyncio.to_thread(
            EffectStudy(root_provider()).queue, offset=offset, limit=limit, max_chars=max_chars
        )

    @mcp.tool()
    async def fx_refine_effect(
        card_id: str,
        updates: dict,
        sources: list[str],
        reason: str,
    ) -> dict:
        """Add names/details newly understood during iteration or Dream.

        Read current card first. Updates are EffectCardInput descriptive fields;
        omitted fields are preserved. Sources use effect_card:, memory:, feedback:
        or dream: followed by an existing ID. A new immutable revision supersedes
        this card; stale heads are refused. Never use this to rename a different
        variant. Archive new representative samples with fx_archive_case, then
        reference that case_id in visual_evidence to improve an old study.
        """
        return await asyncio.to_thread(
            EffectLibrary(root_provider()).refine, card_id, updates, sources, reason
        )

    @mcp.tool()
    async def fx_plan_template_import(source: str, policy: TemplatePolicy | None = None) -> dict:
        """Read mixed HIP/NK directory metadata; no data writes or DCC connection.

        Prunes backup folders and links, groups render sequences, and reports
        cache volume. Preview does not prove project/preview version pairing.
        """
        return await asyncio.to_thread(
            TemplateLibrary(root_provider()).plan, source, policy.model_dump() if policy else None
        )

    @mcp.tool()
    async def fx_learn_templates(
        source: str,
        project_id: str,
        shot_id: str,
        template_key: str = "",
        modules: list[str] | None = None,
        files: list[ArtifactInput] | None = None,
        policy: TemplatePolicy | None = None,
    ) -> dict:
        """Archive saved HIP/HIPLC/HIPNC/NK and statically extract searchable studies.

        Never opens a GUI, evaluates scripts/expressions, changes parameters,
        cooks, renders or writes to source files. Missing caches do not block
        structural study. Scripts and comments are untrusted source data.
        Originals and metadata live in data/cases plus memory.sqlite3.
        Repeat a batch to resume: unchanged inputs are skipped. Explicit files
        select necessary dependencies/preview samples, not whole sequences.
        Unsupported formats are archived as partial, never called fully learned.
        Use a stable template_key to associate a single renamed/moved project.
        """
        cancellation = threading.Event()
        worker = asyncio.create_task(
            asyncio.to_thread(
                TemplateLibrary(root_provider()).ingest,
                source,
                project_id=project_id,
                shot_id=shot_id,
                template_key=template_key,
                modules=modules,
                files=[f.model_dump() for f in files or []],
                policy=policy.model_dump() if policy else None,
                cancellation=cancellation,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            cancellation.set()
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except ArchiveCancelled:
                    break
            with suppress(ArchiveCancelled):
                worker.result()
            raise

    @mcp.tool()
    async def fx_search_templates(
        query: str = "",
        software: str = "",
        module: str = "",
        project_id: str = "",
        include_history: bool = False,
        offset: int = 0,
        limit: int = 10,
        max_chars: int = 6000,
    ) -> dict:
        """Search extracted facts with local BM25; studies are not verified workflows."""

        def search():
            root = root_provider()
            result = TemplateLibrary(root).search(
                query,
                software=software,
                module=module,
                project_id=project_id,
                include_history=include_history,
                offset=offset,
                limit=limit,
                max_chars=max_chars,
            )
            effects = EffectLibrary(root).search(
                query,
                software=software,
                module=module,
                project_id=project_id,
                include_history=include_history,
                limit=3,
                max_chars=max_chars,
            )
            result["effect_matches"] = effects
            # Preserve useful discovery cards when the combined response is small-budget.
            for collection, start, id_key in (
                (result, offset, "study_id"),
                (effects, 0, "card_id"),
            ):
                while collection["items"] and len(compact(result)) > max_chars:
                    if len(collection["items"]) == 1 and not collection["items"][0].get(
                        "detail_required"
                    ):
                        item = collection["items"][0]
                        collection["items"] = [
                            {
                                id_key: item[id_key],
                                "title": item["title"][:80],
                                "detail_required": True,
                            }
                        ]
                    else:
                        collection["items"].pop()
                    collection["next_offset"] = start + len(collection["items"])
                    collection["truncated"] = collection["next_offset"] < collection["total"]
            return result

        return await asyncio.to_thread(search)

    @mcp.tool()
    async def fx_get_template(
        study_id: str,
        section: str = "summary",
        offset: int = 0,
        limit: int = 50,
        max_chars: int = 12000,
    ) -> dict:
        """Read summary, snapshot, nodes, connections or patterns with pagination.

        Large snapshots return a local JSON path and section hints; expand nodes
        and connections by offset. Raw parm/chn/script text preserves original
        expressions and curves; it is not a computed DCC result.
        """

        def read():
            root = root_provider()
            result = TemplateLibrary(root).get(
                study_id, section=section, offset=offset, limit=limit, max_chars=max_chars
            )
            if section == "summary":
                catalog = EffectLibrary(root).catalog(study_id)
                result["effect_catalog"] = catalog
                if len(compact(result)) > max_chars:
                    result = {
                        "study_id": study_id,
                        "effect_catalog": catalog,
                        "detail_required": True,
                        "truncated": True,
                        "next_sections": ["nodes", "dependencies", "history"],
                        "effect_lookup_tool": "fx_search_effects",
                    }
            return result

        return await asyncio.to_thread(read)

    @mcp.tool()
    async def fx_validate_template(study_id: str) -> dict:
        """Validate archived file hashes; does not claim scene/visual reproducibility."""
        return await asyncio.to_thread(TemplateLibrary(root_provider()).validate, study_id)

    @mcp.tool()
    async def fx_export_template(study_id: str, destination: str) -> dict:
        """Copy a study into a new work directory without changing source parameters.

        Refuses an existing destination; restores internal relative file layout.
        External dependencies remain mapped in template-manifest.json; absolute
        file references are preserved and need checking on the work copy.
        """
        return await asyncio.to_thread(
            TemplateLibrary(root_provider()).export, study_id, destination
        )

    @mcp.tool()
    async def fx_learn_template_experience(
        study_id: str,
        title: str,
        method: str,
        reason: str,
        applies_when: str,
        modules: list[str],
        effect_card_id: str = "",
    ) -> dict:
        """Save host-authored interpretation as an inferred candidate linked to a study.

        Cannot promote extracted parameters into verified success. After actual
        use, record feedback, archive the accepted result and finalize through
        the existing learning tools; Dream retains provenance and revisions.
        """
        return await asyncio.to_thread(
            TemplateLibrary(root_provider()).submit_experience,
            study_id,
            title=title,
            method=method,
            reason=reason,
            applies_when=applies_when,
            modules=modules,
            effect_card_id=effect_card_id,
        )

    @mcp.tool()
    async def fx_annotate_template_effects(study_id: str, cards: list[EffectCardInput]) -> dict:
        """Save one text card per effect variant after inspecting the source study.

        Shared scheme_key/node_paths do not merge different effect_key cards.
        Describe subject, appearance, motion, parameter/animation/render/comp
        differences, scope, node evidence and concrete reuse steps. Descriptions
        are host-authored, never auto-inferred from node names. Use supersedes
        for all current heads when revising; originals and source files stay intact.
        """
        return await asyncio.to_thread(
            EffectLibrary(root_provider()).annotate, study_id, [c.model_dump() for c in cards]
        )

    @mcp.tool()
    async def fx_merge_effect_cards(
        card_ids: list[str], card: EffectCardInput, reason: str, apply: bool = False
    ) -> dict:
        """Preview or merge same-effect object copies/render components into one card.

        Read every full current source card first; explain why they compose one
        effect rather than independent looks/motions. Supply a NEW effect_key and
        a host-authored combined description with every source node, input, reuse
        node, preview and visual evidence. Preserve per-layer differences in notes
        and aliases. Default preview does not write. apply atomically creates a
        reference card; originals remain readable/history-searchable. No promotion
        to successful experience. Do not merge merely similar names or shared nodes.
        """
        return await asyncio.to_thread(
            EffectLibrary(root_provider()).merge,
            card_ids,
            card.model_dump(),
            reason,
            apply=apply,
        )

    @mcp.tool()
    async def fx_search_effects(
        query: str = "",
        software: str = "",
        module: str = "",
        project_id: str = "",
        scheme_key: str = "",
        study_id: str = "",
        include_history: bool = False,
        offset: int = 0,
        limit: int = 10,
        max_chars: int = 6000,
    ) -> dict:
        """Find individual looks/motions from natural descriptions such as body energy particles.

        Uses weighted local text retrieval and host-provided aliases, not a vector
        model. Each scheme may have many variants. Open fx_get_effect to compare
        motion, animation and rendering differences before exporting a work copy.
        """
        return await asyncio.to_thread(
            EffectLibrary(root_provider()).search,
            query,
            software=software,
            module=module,
            project_id=project_id,
            scheme_key=scheme_key,
            study_id=study_id,
            include_history=include_history,
            offset=offset,
            limit=limit,
            max_chars=max_chars,
        )

    @mcp.tool()
    async def fx_get_effect(
        card_id: str, section: str = "card", offset: int = 0, max_chars: int = 12000
    ) -> dict:
        """Read a versioned effect card, or paginate its Markdown with section=text.

        Card text is authoritative in SQLite; generated data/effects MDs are views.
        Inspect node paths and evidence, then use fx_export_template(study_id,...).
        Reference cards are not verified successful workflows.
        """
        return await asyncio.to_thread(
            EffectLibrary(root_provider()).get,
            card_id,
            section=section,
            offset=offset,
            max_chars=max_chars,
        )

    @mcp.tool()
    async def fx_review_template_effects(
        study_id: str,
        reviewed_node_paths: list[str] | None = None,
        expected_card_ids: list[str] | None = None,
        exclusions: list[EffectExclusion] | None = None,
        note: str = "",
        media_source: str = "",
        media_reviews: list[MediaReview] | None = None,
    ) -> dict:
        """Preview effect annotation coverage; optionally record an explicit host review.

        To write, provide all reviewed source node paths, current card IDs and a
        reason for each technical node not mapped to a card. A source variant may
        share nodes with others. This records host review, not proof that every
        conceivable parameter combination or visual effect was discovered.
        Revisions/merged branches make previous reviews stale automatically.
        Completion also requires explicit media_source and one media_review per
        inventoried movie/sequence/image. Unavailable or unassigned media are saved
        as issues and prevent completion. Identity questions also block completion.
        """
        return await asyncio.to_thread(
            EffectLibrary(root_provider()).review,
            study_id,
            reviewed_node_paths=reviewed_node_paths,
            expected_card_ids=expected_card_ids,
            exclusions=[x.model_dump() for x in exclusions or []],
            note=note,
            media_source=media_source,
            media_reviews=[x.model_dump() for x in media_reviews or []],
        )
