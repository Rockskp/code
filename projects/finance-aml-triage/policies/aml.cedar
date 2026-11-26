// AgentCore Policy (Cedar) for the Sentinel gateway target. STATUS: DRAFT - not yet validated
// against the AgentCore Policy schema in a live account. Mirrors src/sentinel/policies.py.
// Context attributes are supplied from the tool call arguments + case attributes.

// @id("read-all")
permit (
  principal in Sentinel::Role::"aml_agent",
  action in [Sentinel::Action::"get_alert", Sentinel::Action::"get_customer",
             Sentinel::Action::"get_transactions", Sentinel::Action::"screen_sanctions",
             Sentinel::Action::"risk_signals"],
  resource
);

// @id("agent-close-escalate")
permit (principal in Sentinel::Role::"aml_agent", action == Sentinel::Action::"record_decision", resource)
when { context.outcome == "CLOSE" || context.outcome == "ESCALATE" };

// @id("analyst-close-escalate")
permit (principal in Sentinel::Role::"aml_analyst", action == Sentinel::Action::"record_decision", resource)
when { context.outcome == "CLOSE" || context.outcome == "ESCALATE" };

// @id("officer-any")
permit (principal in Sentinel::Role::"bsa_officer", action == Sentinel::Action::"record_decision", resource);

// @id("agent-no-sar")
forbid (principal in Sentinel::Role::"aml_agent", action == Sentinel::Action::"record_decision", resource)
when { context.outcome == "SAR" };

// @id("agent-no-close-high-risk")
forbid (principal in Sentinel::Role::"aml_agent", action == Sentinel::Action::"record_decision", resource)
when { context.outcome == "CLOSE" && (context.customer_risk == "high" || context.pep) };

// @id("no-close-sanctions-hit")  sanctions_score is passed as an integer basis-points value at the edge
forbid (principal, action == Sentinel::Action::"record_decision", resource)
when { context.outcome == "CLOSE" && context.sanctions_score_bp >= 8500 }
unless { principal in Sentinel::Role::"bsa_officer" };
