// AgentCore Policy (Cedar) for the PriorPath gateway target. STATUS: DRAFT (not validated in a live
// account). Mirrors src/priorpath/policies.py; tests check the @id sets match.

// @id("read-all")
permit (principal in PriorPath::Role::"pa_agent",
        action in [PriorPath::Action::"get_request", PriorPath::Action::"get_eligibility",
                   PriorPath::Action::"get_clinical_record", PriorPath::Action::"check_criteria"],
        resource);

// @id("agent-approve-pend")
permit (principal in PriorPath::Role::"pa_agent", action == PriorPath::Action::"record_decision", resource)
when { context.outcome == "APPROVE" || context.outcome == "PEND_INFO" };

// @id("nurse-approve-pend")
permit (principal in PriorPath::Role::"um_nurse", action == PriorPath::Action::"record_decision", resource)
when { context.outcome == "APPROVE" || context.outcome == "PEND_INFO" };

// @id("md-any")
permit (principal in PriorPath::Role::"medical_director", action == PriorPath::Action::"record_decision", resource);

// @id("deny-md-only")
forbid (principal, action == PriorPath::Action::"record_decision", resource)
when { context.outcome == "DENY" }
unless { principal in PriorPath::Role::"medical_director" };

// @id("agent-no-approve-inactive")
forbid (principal in PriorPath::Role::"pa_agent", action == PriorPath::Action::"record_decision", resource)
when { context.outcome == "APPROVE" && !context.coverage_active };

// @id("no-pend-urgent-unreviewed")
forbid (principal in PriorPath::Role::"pa_agent", action == PriorPath::Action::"record_decision", resource)
when { context.outcome == "PEND_INFO" && context.urgent };
