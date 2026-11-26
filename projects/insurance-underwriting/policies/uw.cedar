// AgentCore Policy (Cedar) for the Appetite gateway target. STATUS: DRAFT (not validated in a live account).
// @id("read-all")
permit (principal in Appetite::Role::"uw_agent",
        action in [Appetite::Action::"get_submission", Appetite::Action::"get_account_history",
                   Appetite::Action::"appetite_check", Appetite::Action::"rate_risk"], resource);
// @id("agent-quote-refer")
permit (principal in Appetite::Role::"uw_agent", action == Appetite::Action::"record_decision", resource)
when { context.outcome == "QUOTE" || context.outcome == "REFER" };
// @id("uw-any")
permit (principal in Appetite::Role::"underwriter", action == Appetite::Action::"record_decision", resource);
// @id("senior-any")
permit (principal in Appetite::Role::"senior_underwriter", action == Appetite::Action::"record_decision", resource);
// @id("send-humans")
permit (principal in [Appetite::Role::"underwriter", Appetite::Role::"senior_underwriter"],
        action == Appetite::Action::"send_quote_to_broker", resource);
// @id("decline-humans-only")
forbid (principal in Appetite::Role::"uw_agent", action == Appetite::Action::"record_decision", resource)
when { context.outcome == "DECLINE" };
// @id("agent-authority-limit")
forbid (principal in Appetite::Role::"uw_agent", action == Appetite::Action::"record_decision", resource)
when { context.outcome == "QUOTE" && (context.tiv > 5000000 || context.premium_cents > 5000000) };
// @id("uw-authority-limit")
forbid (principal in Appetite::Role::"underwriter", action == Appetite::Action::"record_decision", resource)
when { context.outcome == "QUOTE" && context.tiv > 25000000 };
