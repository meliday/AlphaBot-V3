import math
from pathlib import Path
from alpha_bot.models import NewsAssessment, Candle
from alpha_bot.strategy import StrategyAnalyzer
from alpha_bot.data.providers import FixtureDataProvider

# ── Aggressive Experiment Parameters ──────────────────────────────────
CONVICTION_THRESHOLD = 27
STOP_LOOSEN_FACTOR = 1.5      # 고확신 시 손절 폭 1.5배 확대
AVERAGE_DOWN_TRIGGER = -0.05  # -5% 하락 시 추가 매수
# ─────────────────────────────────────────────────────────────────────

class AggressiveAnalyzer(StrategyAnalyzer):
    def analyze(self, *args, **kwargs):
        # 유저가 지적한 대로 LLM 가산점 +3 상시 반영
        kwargs['news_assessment'] = NewsAssessment(
            sentiment="positive", severity="high", earnings_caution=False,
            score_adjustment=3, reasoning="HBM4 독점 공급 및 실적 퀀텀 점프 기대"
        )
        return super().analyze(*args, **kwargs)

def run_aggressive_test():
    provider = FixtureDataProvider(Path("data"))
    ticker, market = "000660", "KR"
    candles = provider.get_candles(ticker, market, lookback=500)
    fundamentals = provider.get_fundamentals(ticker, market)
    context = provider.get_market_context(ticker, market)
    analyzer = AggressiveAnalyzer(min_score=24, min_rr=1.5)

    index = 220
    total_pnl = 0.0
    trades = []
    
    while index < len(candles) - 1:
        asof = candles[index].date
        report = analyzer.analyze(ticker, market, candles[:index+1], fundamentals, [], context)
        
        if report.signal not in {"Buy", "Strong Buy"}:
            index += 1
            continue

        # --- 진입 (1차 매수) ---
        entry_idx = index + 1
        entry_price = candles[entry_idx].open
        score = report.scoreboard.total
        
        # 고확신 시 손절 라인 완화 로직
        stop_dist = entry_price - report.trade_plan.stop_loss
        if score >= CONVICTION_THRESHOLD:
            stop_dist *= STOP_LOOSEN_FACTOR
        
        stop_loss = entry_price - stop_dist
        target = report.trade_plan.target1
        
        # 포지션 상태
        units = 1
        avg_price = entry_price
        has_averaged_down = False
        exit_idx = None
        outcome = ""

        # --- 감시 루프 (물타기 및 청산) ---
        for j in range(entry_idx + 1, len(candles)):
            curr = candles[j]
            
            # 1. 물타기 체크 (고확신일 때만 1회 수행)
            if score >= CONVICTION_THRESHOLD and not has_averaged_down:
                unrealized_pnl = (curr.low / avg_price) - 1
                if unrealized_pnl <= AVERAGE_DOWN_TRIGGER:
                    # 물타기 실행 (동일 수량 추가 매수)
                    add_price = avg_price * (1 + AVERAGE_DOWN_TRIGGER) # 트리거 가격에 체결 가정
                    avg_price = (avg_price + add_price) / 2
                    units = 2
                    has_averaged_down = True
                    # 물타기 후에는 손절 라인을 평단 기준으로 재설정 (평단 대비 기존 stop_dist 비율 유지)
                    # (실전에서는 더 복잡하지만 여기서는 단순화)
            
            # 2. 청산 체크 (Gap down 고려)
            if curr.open <= stop_loss:
                exit_idx, exit_price, outcome = j, curr.open, "stop_gap"
                break
            if curr.low <= stop_loss:
                exit_idx, exit_price, outcome = j, stop_loss, "stop"
                break
            if curr.high >= target:
                exit_idx, exit_price, outcome = j, target, "target"
                break
            if j >= entry_idx + 30: # Max hold 30 days
                exit_idx, exit_price, outcome = j, curr.close, "time"
                break
        
        if exit_idx:
            pnl_pct = (exit_price / avg_price - 1) * 100
            trades.append({
                "entry": entry_idx, "exit": exit_idx, 
                "pnl": pnl_pct, "outcome": outcome, 
                "avg_price": avg_price, "exit_price": exit_price,
                "averaged": has_averaged_down
            })
            total_pnl += pnl_pct
            index = exit_idx + 1
        else:
            index += 1

    print(f"=== ⚡ 공격적 전략 (물타기+손절완화) 결과 ===")
    print(f"매매 건수: {len(trades)}건, 누적 수익률: {total_pnl:.1f}%")
    for t in trades:
        e_date = candles[t['entry']].date
        x_date = candles[t['exit']].date
        avg_tag = "[물타기함]" if t['averaged'] else "[단일진입]"
        print(f"  {e_date} -> {x_date} {avg_tag} {t['outcome']} 리턴: {t['pnl']:.1f}% (평단: {t['avg_price']:,.0f}, 매도가: {t['exit_price']:,.0f})")

if __name__ == "__main__":
    run_aggressive_test()
