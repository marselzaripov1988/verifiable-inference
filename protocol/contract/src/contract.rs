use cosmwasm_std::{
    entry_point, from_json, to_json_binary, Addr, BankMsg, Binary, Coin, CosmosMsg, Deps, DepsMut,
    Env, MessageInfo, Order, Response, StdResult, Uint128, WasmMsg,
};

use crate::error::ContractError;
use crate::msg::{
    ChecksResponse, ConformanceRef, Cw20Execute, Cw20ReceiveMsg, ExecuteMsg, InstantiateMsg,
    MigrateMsg, QueryMsg, ReceiveHook,
};
use crate::state::{
    Check, Config, ConfigV1, Conformance, Executor, ExecutorStatus, Forfeit, Job, JobState, Micro,
    MicroLog, Settlement, CHECKS, CONFIG, CURRENT_VERSION, EXECUTORS, JOBS, NEXT_JOB,
    STATE_VERSION,
};

/// Плата проверяющим от цены. Деление ВВЕРХ.
///
/// Вверх, а не вниз, потому что эту плату вносит заказчик сверх цены, и
/// недобор означал бы, что проверяющим не хватило на доли. Лишние
/// единицы, не разошедшиеся по долям, достаются получателю цены.
fn fee_for(price: Uint128, bps: u16) -> Uint128 {
    let num = price * Uint128::from(bps as u128);
    let den = Uint128::from(10_000u128);
    (num + den - Uint128::one()) / den
}

/// Выплата в единице расчёта. ЕДИНСТВЕННОЕ место, которое знает, чем
/// идёт расчёт: иначе добавление родной монеты означало бы правку в
/// каждом месте, где двигаются деньги, и одно из них забылось бы.
fn pay(cfg: &Config, to: &Addr, amount: Uint128) -> StdResult<CosmosMsg> {
    match &cfg.settlement {
        Settlement::Native(denom) => Ok(BankMsg::Send {
            to_address: to.to_string(),
            amount: vec![Coin {
                denom: denom.clone(),
                amount,
            }],
        }
        .into()),
        Settlement::Cw20(token) => cw20_transfer(token, to, amount),
    }
}

/// Перевод CW20. Собирается вручную: тащить зависимость cw20 ради одного
/// сообщения незачем, а формат фиксирован стандартом.
fn cw20_transfer(token: &Addr, to: &Addr, amount: Uint128) -> StdResult<CosmosMsg> {
    Ok(WasmMsg::Execute {
        contract_addr: token.to_string(),
        msg: to_json_binary(&Cw20Execute::Transfer {
            recipient: to.to_string(),
            amount,
        })?,
        funds: vec![],
    }
    .into())
}
use sha2::{Digest, Sha256};

/// Канонический хэш вектора: каждый элемент как i64 little-endian,
/// восемь байт, подряд. Ни длины, ни разделителей — представление должно
/// быть единственным, иначе два честных узла получат разные хэши от
/// одного вектора.
pub fn commit_hash(v: &[MicroLog]) -> String {
    let mut h = Sha256::new();
    for x in v {
        h.update(x.to_le_bytes());
    }
    hex_lower(&h.finalize())
}

fn hex_lower(b: &[u8]) -> String {
    let mut s = String::with_capacity(b.len() * 2);
    for byte in b {
        s.push_str(&format!("{:02x}", byte));
    }
    s
}

/// Расхождение: среднее абсолютное по общей длине, в микродолях.
///
/// Делится целочисленно — иначе на цепочке не посчитать, а округление
/// вниз работает в пользу исполнителя, что правильнее обратного: лучше
/// пропустить на границе, чем обвинить честного.
///
/// Разная длина уликой не считается: жадное декодирование может
/// оборваться по лимиту токенов в разный момент.
pub fn divergence_micro(a: &[MicroLog], b: &[MicroLog]) -> Result<Micro, ContractError> {
    let n = a.len().min(b.len());
    if n == 0 {
        return Err(ContractError::EmptyVector);
    }
    let mut sum: u128 = 0;
    for i in 0..n {
        sum += (a[i] as i128 - b[i] as i128).unsigned_abs() as u128;
    }
    Ok((sum / n as u128) as Micro)
}

#[entry_point]
pub fn instantiate(
    deps: DepsMut,
    _env: Env,
    _info: MessageInfo,
    msg: InstantiateMsg,
) -> Result<Response, ContractError> {
    if msg.checks_required == 0 {
        return Err(ContractError::ZeroChecks);
    }
    if msg.verifier_fee_bps > 10_000 {
        return Err(ContractError::BadFeeBps {
            bps: msg.verifier_fee_bps,
        });
    }
    // Пустой эталон означал бы, что пробу проходят не считая ничего.
    if msg.conformance_lens.is_empty() {
        return Err(ContractError::EmptyProbe);
    }
    let cfg = Config {
        admin: deps.api.addr_validate(&msg.admin)?,
        checks_required: msg.checks_required,
        pinned_model: msg.pinned_model,
        pinned_runtime: msg.pinned_runtime,
        pinned_dtype: msg.pinned_dtype,
        pinned_arch: msg.pinned_arch,
        kernel_cache_hash: msg.kernel_cache_hash,
        conformance_limit_micro: msg.conformance_limit_micro,
        conformance_ref_hash: msg.conformance_ref_hash,
        conformance_lens: msg.conformance_lens,
        conformance_version: 1,
        // Адрес CW20 проверяется здесь: в сообщении он приходит как
        // Addr, а Addr из сообщения НЕ проверен — это известная ловушка
        // cosmwasm, и неверный адрес иначе всплыл бы при первой выплате.
        settlement: match msg.settlement {
            Settlement::Cw20(a) => {
                Settlement::Cw20(deps.api.addr_validate(a.as_str())?)
            }
            native => native,
        },
        stake_denom: msg.stake_denom,
        min_stake: msg.min_stake,
        verifier_fee_bps: msg.verifier_fee_bps,
        forfeit: msg.forfeit,
    };
    CONFIG.save(deps.storage, &cfg)?;
    NEXT_JOB.save(deps.storage, &1u64)?;
    STATE_VERSION.save(deps.storage, &CURRENT_VERSION)?;
    Ok(Response::new().add_attribute("action", "instantiate"))
}

/// Перенос хранилища на этот код.
///
/// Принимает только переход вперёд и только по известным формам. Чего
/// здесь нарочно НЕТ: перехода «как-нибудь», то есть попытки разобрать
/// настройки нынешней формой и, если не вышло, прежней. Такой перенос
/// молча проглотил бы незнакомое хранилище, а молчание на хранилище
/// живой сети хуже отказа.
///
/// Отсутствие записи о версии означает форму 1: октябрьский код её не
/// писал. Это не догадка — другого кода у этих адресов не было.
#[entry_point]
pub fn migrate(deps: DepsMut, _env: Env, _msg: MigrateMsg) -> Result<Response, ContractError> {
    let was = STATE_VERSION.may_load(deps.storage)?.unwrap_or(1);
    if was == CURRENT_VERSION {
        // Тот же код поверх себя. Не ошибка: так выглядит повторная
        // отправка переноса, и она обязана быть безвредной.
        return Ok(Response::new()
            .add_attribute("action", "migrate")
            .add_attribute("from", was.to_string())
            .add_attribute("to", CURRENT_VERSION.to_string())
            .add_attribute("changed", "nothing"));
    }
    if was > CURRENT_VERSION {
        return Err(ContractError::MigrateBackwards {
            have: was,
            code: CURRENT_VERSION,
        });
    }
    if was != 1 {
        return Err(ContractError::MigrateUnknown { have: was });
    }
    // Форма 1 -> 2: из настроек уходит `attestor`. Читается прежней
    // структурой, потому что нынешняя запрещает неизвестные поля и на
    // этих байтах разбор провалился бы.
    let raw = deps
        .storage
        .get(b"config")
        .ok_or(ContractError::MigrateNoConfig)?;
    let old: ConfigV1 = from_json(&raw)?;
    CONFIG.save(deps.storage, &old.migrate())?;
    STATE_VERSION.save(deps.storage, &CURRENT_VERSION)?;
    Ok(Response::new()
        .add_attribute("action", "migrate")
        .add_attribute("from", "1")
        .add_attribute("to", CURRENT_VERSION.to_string())
        .add_attribute("changed", "attestor_dropped"))
}

#[entry_point]
pub fn execute(
    deps: DepsMut,
    _env: Env,
    info: MessageInfo,
    msg: ExecuteMsg,
) -> Result<Response, ContractError> {
    match msg {
        ExecuteMsg::UpdateConfig {
            admin,
            checks_required,
            pinned_model,
            pinned_runtime,
            pinned_dtype,
            pinned_arch,
            kernel_cache_hash,
            conformance_limit_micro,
            conformance_ref,
            settlement,
            stake_denom,
            min_stake,
            verifier_fee_bps,
            forfeit,
        } => update_config(
            deps,
            info,
            admin,
            checks_required,
            pinned_model,
            pinned_runtime,
            pinned_dtype,
            pinned_arch,
            kernel_cache_hash,
            conformance_limit_micro,
            conformance_ref,
            settlement,
            stake_denom,
            min_stake,
            verifier_fee_bps,
            forfeit,
        ),
        ExecuteMsg::RegisterExecutor {
            model,
            runtime,
            dtype,
            card_class,
            kernel_cache_hash,
        } => register_executor(
            deps,
            info,
            model,
            runtime,
            dtype,
            card_class,
            kernel_cache_hash,
        ),
        ExecuteMsg::SetSuspended {
            executor,
            suspended,
        } => set_suspended(deps, info, executor, suspended),
        ExecuteMsg::DepositStake {} => deposit_stake(deps, info),
        ExecuteMsg::WithdrawStake { amount } => withdraw_stake(deps, info, amount),
        ExecuteMsg::SubmitJob { prompt_hash, price } => {
            submit_job_native(deps, info, prompt_hash, price)
        }
        ExecuteMsg::Receive(r) => receive_cw20(deps, info, r),
        ExecuteMsg::SubmitConformance {
            divergence_micro,
            logprobs_hash,
        } => submit_conformance(deps, info, divergence_micro, logprobs_hash),
        ExecuteMsg::DisputeConformance {
            executor,
            reference_logprobs,
            executor_logprobs,
        } => dispute_conformance(deps, executor, reference_logprobs, executor_logprobs),
        ExecuteMsg::ClaimJob { job_id } => claim_job(deps, info, job_id),
        ExecuteMsg::SubmitAnswer {
            job_id,
            answer_hash,
            logprobs_hash,
            answer_tokens,
        } => submit_answer(deps, info, job_id, answer_hash, logprobs_hash, answer_tokens),
        ExecuteMsg::SubmitCheck {
            job_id,
            divergence_micro,
            logprobs_hash,
        } => submit_check(deps, info, job_id, divergence_micro, logprobs_hash),
        ExecuteMsg::Finalize { job_id } => finalize(deps, job_id),
        ExecuteMsg::ResolveDispute {
            job_id,
            checker,
            producer_logprobs,
            checker_logprobs,
        } => resolve_dispute(deps, job_id, checker, producer_logprobs, checker_logprobs),
    }
}

#[allow(clippy::too_many_arguments)]
fn update_config(
    deps: DepsMut,
    info: MessageInfo,
    admin: Option<String>,
    checks_required: Option<u32>,
    pinned_model: Option<String>,
    pinned_runtime: Option<String>,
    pinned_dtype: Option<String>,
    pinned_arch: Option<String>,
    kernel_cache_hash: Option<String>,
    conformance_limit_micro: Option<Micro>,
    conformance_ref: Option<ConformanceRef>,
    settlement: Option<Settlement>,
    stake_denom: Option<String>,
    min_stake: Option<Uint128>,
    verifier_fee_bps: Option<u16>,
    forfeit: Option<Forfeit>,
) -> Result<Response, ContractError> {
    let mut cfg = CONFIG.load(deps.storage)?;
    if info.sender != cfg.admin {
        return Err(ContractError::NotAdmin);
    }
    // Смена администратора. Передаётся последней правкой: после неё
    // править настройки сможет только новый адрес.
    if let Some(a) = admin {
        cfg.admin = deps.api.addr_validate(&a)?;
    }
    if let Some(c) = checks_required {
        if c == 0 {
            return Err(ContractError::ZeroChecks);
        }
        cfg.checks_required = c;
    }
    // Смена связки или эталона обнуляет все выданные пробы: проба
    // свидетельствует о соответствии КОНКРЕТНОЙ настройке. Иначе узел,
    // прошедший пробу до смены, продолжал бы работать с ней после.
    let mut rotate = false;
    if let Some(m) = pinned_model {
        rotate |= m != cfg.pinned_model;
        cfg.pinned_model = m;
    }
    if let Some(r) = pinned_runtime {
        rotate |= r != cfg.pinned_runtime;
        cfg.pinned_runtime = r;
    }
    if let Some(d) = pinned_dtype {
        rotate |= d != cfg.pinned_dtype;
        cfg.pinned_dtype = d;
    }
    // Архитектура и кеш ядер — такая же часть связки, как модель: при
    // другом кеше узлы класса считают по-разному в последнем бите.
    if let Some(a) = pinned_arch {
        rotate |= a != cfg.pinned_arch;
        cfg.pinned_arch = a;
    }
    if let Some(k) = kernel_cache_hash {
        rotate |= k != cfg.kernel_cache_hash;
        cfg.kernel_cache_hash = k;
    }
    if let Some(l) = conformance_limit_micro {
        cfg.conformance_limit_micro = l;
    }
    if let Some(r) = conformance_ref {
        if r.lens.is_empty() {
            return Err(ContractError::EmptyProbe);
        }
        rotate |= r.ref_hash != cfg.conformance_ref_hash || r.lens != cfg.conformance_lens;
        cfg.conformance_ref_hash = r.ref_hash;
        cfg.conformance_lens = r.lens;
    }
    if rotate {
        cfg.conformance_version += 1;
    }
    if let Some(st) = settlement {
        cfg.settlement = match st {
            Settlement::Cw20(a) => {
                Settlement::Cw20(deps.api.addr_validate(a.as_str())?)
            }
            native => native,
        };
    }
    if let Some(d) = stake_denom {
        cfg.stake_denom = d;
    }
    if let Some(m) = min_stake {
        cfg.min_stake = m;
    }
    if let Some(b) = verifier_fee_bps {
        if b > 10_000 {
            return Err(ContractError::BadFeeBps { bps: b });
        }
        cfg.verifier_fee_bps = b;
    }
    if let Some(f) = forfeit {
        cfg.forfeit = f;
    }
    CONFIG.save(deps.storage, &cfg)?;
    Ok(Response::new()
        .add_attribute("action", "update_config")
        .add_attribute("conformance_version", cfg.conformance_version.to_string()))
}

fn register_executor(
    deps: DepsMut,
    info: MessageInfo,
    model: String,
    runtime: String,
    dtype: String,
    card_class: String,
    kernel_cache_hash: String,
) -> Result<Response, ContractError> {
    let cfg = CONFIG.load(deps.storage)?;
    // Связка проверяется ЗДЕСЬ, а не при выдаче задания: вердикт
    // действителен только внутри класса, и пускать чужую версию внутрь,
    // чтобы отклонить позже, незачем.
    if model != cfg.pinned_model || runtime != cfg.pinned_runtime || dtype != cfg.pinned_dtype {
        return Err(ContractError::WrongPinning {
            model,
            runtime,
            dtype,
        });
    }
    // Архитектура и кеш ядер — остальная часть класса.
    //
    // Заявление узла само по себе ничего не доказывает, и проверить его
    // контракт не может. Доказывает проба: при нулевом допуске узел с
    // другими ядрами на ней не сойдётся. Здесь отсекается явное
    // несоответствие, чтобы такой узел не дошёл до пробы впустую.
    if card_class != cfg.pinned_arch || kernel_cache_hash != cfg.kernel_cache_hash {
        return Err(ContractError::WrongClass {
            arch: card_class,
            kernel_cache_hash,
        });
    }
    if EXECUTORS.has(deps.storage, &info.sender) {
        return Err(ContractError::ExecutorExists);
    }
    EXECUTORS.save(
        deps.storage,
        &info.sender,
        &Executor {
            model,
            runtime,
            dtype,
            card_class,
            kernel_cache_hash,
            // Сразу Active, и это НЕ ослабление допуска. Залог и проба
            // проверяются при каждом взятии задания (см.
            // active_executor), так что зарегистрировавшийся без залога
            // и без пробы не получит ничего. Прежде здесь стоял Pending
            // и ждал подписи аттестора — единственного нецепочечного
            // условия во всём правиле. Решено 26 октября: условие,
            // которое проверяет человек, заменено условиями, которые
            // проверяет цепочка.
            status: ExecutorStatus::Active,
            jobs_done: 0,
            convictions: 0,
            stake: Uint128::zero(),
            conformance: None,
        },
    )?;
    Ok(Response::new()
        .add_attribute("action", "register_executor")
        .add_attribute("executor", info.sender))
}

fn set_suspended(
    deps: DepsMut,
    info: MessageInfo,
    executor: String,
    suspended: bool,
) -> Result<Response, ContractError> {
    let cfg = CONFIG.load(deps.storage)?;
    if info.sender != cfg.admin {
        return Err(ContractError::NotAdmin);
    }
    let addr = deps.api.addr_validate(&executor)?;
    let mut e = EXECUTORS
        .may_load(deps.storage, &addr)?
        .ok_or(ContractError::ExecutorUnknown)?;
    if e.status == ExecutorStatus::Banned {
        return Err(ContractError::ExecutorBanned);
    }
    e.status = if suspended {
        ExecutorStatus::Suspended
    } else {
        ExecutorStatus::Active
    };
    EXECUTORS.save(deps.storage, &addr, &e)?;
    Ok(Response::new().add_attribute("action", "set_suspended"))
}

fn deposit_stake(deps: DepsMut, info: MessageInfo) -> Result<Response, ContractError> {
    let cfg = CONFIG.load(deps.storage)?;
    let sent = info
        .funds
        .iter()
        .find(|c| c.denom == cfg.stake_denom)
        .map(|c| c.amount)
        .unwrap_or_default();
    if sent.is_zero() {
        return Err(ContractError::WrongStakeDenom {
            want: cfg.stake_denom.clone(),
            got: info
                .funds
                .iter()
                .map(|c| c.denom.clone())
                .collect::<Vec<_>>()
                .join(","),
        });
    }
    let mut e = EXECUTORS
        .may_load(deps.storage, &info.sender)?
        .ok_or(ContractError::ExecutorUnknown)?;
    e.stake += sent;
    EXECUTORS.save(deps.storage, &info.sender, &e)?;
    Ok(Response::new()
        .add_attribute("action", "deposit_stake")
        .add_attribute("stake", e.stake.to_string()))
}

fn withdraw_stake(
    deps: DepsMut,
    info: MessageInfo,
    amount: Uint128,
) -> Result<Response, ContractError> {
    let cfg = CONFIG.load(deps.storage)?;
    let mut e = EXECUTORS
        .may_load(deps.storage, &info.sender)?
        .ok_or(ContractError::ExecutorUnknown)?;
    // Забанить и дать уйти с залогом — значит не наказать вовсе.
    if e.status == ExecutorStatus::Banned {
        return Err(ContractError::ExecutorBanned);
    }
    // Незакрытые задания держат залог: иначе его забирают ровно перед
    // вердиктом, и наказывать становится нечем.
    let open = count_open_jobs(deps.as_ref(), &info.sender)?;
    if open > 0 {
        return Err(ContractError::StakeLocked { open });
    }
    if amount > e.stake {
        return Err(ContractError::StakeInsufficient {
            have: e.stake.to_string(),
            want: amount.to_string(),
        });
    }
    e.stake -= amount;
    // Опустился ниже минимума — больше не активен, но и не наказан.
    if e.stake < cfg.min_stake && e.status == ExecutorStatus::Active {
        e.status = ExecutorStatus::Pending;
    }
    EXECUTORS.save(deps.storage, &info.sender, &e)?;
    Ok(Response::new()
        .add_message(BankMsg::Send {
            to_address: info.sender.to_string(),
            amount: vec![Coin {
                denom: cfg.stake_denom,
                amount,
            }],
        })
        .add_attribute("action", "withdraw_stake")
        .add_attribute("stake", e.stake.to_string()))
}

/// Сколько у исполнителя заданий, по которым вердикт ещё не вынесен.
fn count_open_jobs(deps: Deps, who: &Addr) -> StdResult<u64> {
    let mut n = 0u64;
    for r in JOBS.range(deps.storage, None, None, Order::Ascending) {
        let (_, j) = r?;
        if j.executor.as_ref() == Some(who) && j.state != JobState::Settled {
            n += 1;
        }
    }
    Ok(n)
}

/// Приём CW20: расчётная единица не нативна, и это единственный путь, по
/// которому монеты попадают в контракт вместе с заданием.
fn receive_cw20(
    deps: DepsMut,
    info: MessageInfo,
    r: Cw20ReceiveMsg,
) -> Result<Response, ContractError> {
    let cfg = CONFIG.load(deps.storage)?;
    // Вызов обязан прийти ОТ контракта расчётной единицы: иначе любой
    // мог бы объявить задание оплаченным своим токеном.
    let token = match &cfg.settlement {
        Settlement::Cw20(t) => t.clone(),
        // При расчёте родной монетой платёж CW20 заданием не становится:
        // иначе задание оказалось бы оплачено не тем, чем платят все.
        Settlement::Native(_) => return Err(ContractError::NotCw20Settlement),
    };
    if info.sender != token {
        return Err(ContractError::WrongCw20 {
            want: token.to_string(),
            got: info.sender.to_string(),
        });
    }
    if r.amount.is_zero() {
        return Err(ContractError::ZeroPrice);
    }
    match from_json(&r.msg)? {
        ReceiveHook::SubmitJob { prompt_hash, price } => {
            let client = deps.api.addr_validate(&r.sender)?;
            create_job(deps, &cfg, client, prompt_hash, price, r.amount)
        }
    }
}

/// Поставить задание. Одна сборка на оба вида расчёта: раздвоив её, я
/// бы раздвоил и жизненный цикл задания.
///
/// `price` — то, что получит исполнитель; `paid` — то, что внёс
/// заказчик. Они обязаны различаться ровно на плату проверяющим:
/// меньше — проверки нечем оплатить, больше — остаток завис бы на
/// контракте, и вернуть его было бы некому.
fn create_job(
    deps: DepsMut,
    cfg: &Config,
    client: Addr,
    prompt_hash: String,
    price: Uint128,
    paid: Uint128,
) -> Result<Response, ContractError> {
    if price.is_zero() {
        return Err(ContractError::ZeroPrice);
    }
    let fee_total = fee_for(price, cfg.verifier_fee_bps);
    let want = price + fee_total;
    if paid != want {
        return Err(ContractError::WrongTotal {
            want: want.to_string(),
            got: paid.to_string(),
            price: price.to_string(),
            fee: fee_total.to_string(),
        });
    }
    let id = NEXT_JOB.load(deps.storage)?;
    JOBS.save(
        deps.storage,
        id,
        &Job {
            client,
            prompt_hash,
            executor: None,
            state: JobState::Open,
            answer_hash: None,
            logprobs_hash: None,
            answer_tokens: None,
            guilty: None,
            reveal_verified: None,
            price,
            fee_total,
            paid: false,
        },
    )?;
    NEXT_JOB.save(deps.storage, &(id + 1))?;
    Ok(Response::new()
        .add_attribute("action", "submit_job")
        .add_attribute("job_id", id.to_string())
        .add_attribute("price", price.to_string())
        .add_attribute("verifier_fee", fee_total.to_string())
        .add_attribute("total", paid.to_string()))
}

/// Задание, оплаченное РОДНОЙ монетой: она приходит с транзакцией.
fn submit_job_native(
    deps: DepsMut,
    info: MessageInfo,
    prompt_hash: String,
    price: Uint128,
) -> Result<Response, ContractError> {
    let cfg = CONFIG.load(deps.storage)?;
    let denom = match &cfg.settlement {
        Settlement::Native(d) => d.clone(),
        // При расчёте CW20 монеты приходят только через Receive, и
        // принимать их ещё и так значило бы два пути для одних денег.
        Settlement::Cw20(t) => {
            return Err(ContractError::NotNativeSettlement {
                want: t.to_string(),
            })
        }
    };
    // Ровно одна монета и ровно того денoma. Чужая монета рядом с нужной
    // осталась бы в контракте навсегда: вернуть её некому и нечем.
    let sent: Uint128 = info
        .funds
        .iter()
        .filter(|c| c.denom == denom)
        .map(|c| c.amount)
        .sum();
    if info.funds.len() != 1 || sent.is_zero() {
        let got = if info.funds.is_empty() {
            "ничего".to_string()
        } else {
            info.funds
                .iter()
                .map(|c| format!("{}{}", c.amount, c.denom))
                .collect::<Vec<_>>()
                .join(",")
        };
        return Err(ContractError::WrongSettlementFunds { want: denom, got });
    }
    let client = info.sender;
    create_job(deps, &cfg, client, prompt_hash, price, sent)
}

fn active_executor(deps: &DepsMut, who: &Addr) -> Result<Executor, ContractError> {
    let e = EXECUTORS
        .may_load(deps.storage, who)?
        .ok_or(ContractError::ExecutorUnknown)?;
    if e.status != ExecutorStatus::Active {
        return Err(ContractError::ExecutorNotActive {
            status: format!("{:?}", e.status),
        });
    }
    // Залог проверяется при ВЗЯТИИ задания, а не только при допуске:
    // иначе исполнитель забирает залог после допуска и работает без него.
    let cfg = CONFIG.load(deps.storage)?;
    if e.stake < cfg.min_stake {
        return Err(ContractError::StakeTooSmall {
            have: e.stake.to_string(),
            need: cfg.min_stake.to_string(),
        });
    }
    // Проба проверяется при ВЗЯТИИ задания по той же причине, что и
    // залог: иначе достаточно пройти её один раз и потом сменить режим
    // исполнения. Свежесть — через версию эталона.
    conformant(deps.as_ref(), &cfg, who)?;
    Ok(e)
}

/// Максимум среднего расхождения ПО ТЕКСТАМ пробы.
///
/// Склейка режется по длинам эталона. Почему максимум, а не среднее по
/// склейке: неверный режим исполнения портит РЕДКИЕ тексты, и среднее их
/// размывает — замерено, что зазор падает с 20x до 1,5x.
fn max_per_text(
    reference: &[MicroLog],
    theirs: &[MicroLog],
    lens: &[u32],
) -> Result<Micro, ContractError> {
    let want: usize = lens.iter().map(|&n| n as usize).sum();
    if want == 0 {
        return Err(ContractError::EmptyProbe);
    }
    // Длины заданы текстами пробы: оценивается готовый текст, обрываться
    // ему негде. Поэтому расхождение в длине — не допуск, а признак, что
    // оценивалось не то.
    for v in [reference, theirs] {
        if v.len() != want {
            return Err(ContractError::ConformanceShape {
                got: v.len() as u64,
                want: want as u64,
            });
        }
    }
    let mut worst: Micro = 0;
    let mut at = 0usize;
    for &n in lens {
        let n = n as usize;
        let d = divergence_micro(&reference[at..at + n], &theirs[at..at + n])?;
        worst = worst.max(d);
        at += n;
    }
    Ok(worst)
}

fn submit_conformance(
    deps: DepsMut,
    info: MessageInfo,
    divergence_micro: Micro,
    logprobs_hash: String,
) -> Result<Response, ContractError> {
    let cfg = CONFIG.load(deps.storage)?;
    let mut e = EXECUTORS
        .may_load(deps.storage, &info.sender)?
        .ok_or(ContractError::ExecutorUnknown)?;
    if e.status == ExecutorStatus::Banned {
        return Err(ContractError::ExecutorBanned);
    }
    if divergence_micro > cfg.conformance_limit_micro {
        return Err(ContractError::ConformanceFailed {
            got: divergence_micro,
            limit: cfg.conformance_limit_micro,
        });
    }
    e.conformance = Some(Conformance {
        divergence_micro,
        logprobs_hash,
        ref_version: cfg.conformance_version,
    });
    EXECUTORS.save(deps.storage, &info.sender, &e)?;
    Ok(Response::new()
        .add_attribute("action", "submit_conformance")
        .add_attribute("executor", info.sender)
        .add_attribute("divergence_micro", divergence_micro.to_string())
        .add_attribute("ref_version", cfg.conformance_version.to_string()))
}

fn dispute_conformance(
    deps: DepsMut,
    executor: String,
    reference_logprobs: Vec<MicroLog>,
    executor_logprobs: Vec<MicroLog>,
) -> Result<Response, ContractError> {
    let cfg = CONFIG.load(deps.storage)?;
    let addr = deps.api.addr_validate(&executor)?;
    let mut e = EXECUTORS
        .may_load(deps.storage, &addr)?
        .ok_or(ContractError::ExecutorUnknown)?;
    let c = e.conformance.clone().ok_or(ContractError::ConformanceMissing)?;

    // Оба набора сверяются с обязательствами ДО пересчёта: иначе
    // опровергнуть можно было бы подсунутыми числами.
    let got = commit_hash(&reference_logprobs);
    if got != cfg.conformance_ref_hash {
        return Err(ContractError::RevealMismatch {
            got,
            want: cfg.conformance_ref_hash,
        });
    }
    let got = commit_hash(&executor_logprobs);
    if got != c.logprobs_hash {
        return Err(ContractError::RevealMismatch {
            got,
            want: c.logprobs_hash,
        });
    }

    let actual = max_per_text(&reference_logprobs, &executor_logprobs, &cfg.conformance_lens)?;
    let lied = actual > cfg.conformance_limit_micro;
    if lied {
        // Назвать верное число у него было всё: и эталон, и свои векторы.
        // Пересчёт однозначен, чужой оценки здесь нет — поэтому и
        // наказание прямое, в отличие от спора по заданию.
        e.conformance = None;
        e.convictions += 1;
        e.status = ExecutorStatus::Banned;
    } else {
        // Проба подтвердилась: число, которое он назвал, не меньше
        // пересчитанного по его же векторам.
        e.conformance = Some(Conformance {
            divergence_micro: actual,
            ..c
        });
    }
    EXECUTORS.save(deps.storage, &addr, &e)?;
    Ok(Response::new()
        .add_attribute("action", "dispute_conformance")
        .add_attribute("executor", addr)
        .add_attribute("claimed_micro", c.divergence_micro.to_string())
        .add_attribute("actual_micro", actual.to_string())
        .add_attribute("lied", lied.to_string()))
}

fn claim_job(deps: DepsMut, info: MessageInfo, job_id: u64) -> Result<Response, ContractError> {
    active_executor(&deps, &info.sender)?;
    let mut job = JOBS
        .may_load(deps.storage, job_id)?
        .ok_or(ContractError::JobUnknown)?;
    if job.state != JobState::Open {
        return Err(ContractError::WrongJobState {
            state: format!("{:?}", job.state),
            expected: "Open".into(),
        });
    }
    job.executor = Some(info.sender.clone());
    job.state = JobState::Claimed;
    JOBS.save(deps.storage, job_id, &job)?;
    Ok(Response::new()
        .add_attribute("action", "claim_job")
        .add_attribute("job_id", job_id.to_string()))
}

fn submit_answer(
    deps: DepsMut,
    info: MessageInfo,
    job_id: u64,
    answer_hash: String,
    logprobs_hash: String,
    answer_tokens: u32,
) -> Result<Response, ContractError> {
    // Настройки здесь больше не нужны: гейт по длине ответа убран, а он
    // был единственным, что отсюда их читало.
    let mut job = JOBS
        .may_load(deps.storage, job_id)?
        .ok_or(ContractError::JobUnknown)?;
    if job.state != JobState::Claimed {
        return Err(ContractError::WrongJobState {
            state: format!("{:?}", job.state),
            expected: "Claimed".into(),
        });
    }
    if job.executor.as_ref() != Some(&info.sender) {
        return Err(ContractError::NotYourJob);
    }
    // Гейта по длине ответа больше нет. Он стоял затем, что на коротких
    // ответах запас порога падал; порога нет, а равенство хэшей от длины
    // не зависит. Замерено и прямо: отсечка по длине ответа пересечения
    // полос не убирала ни при 32 токенах, ни при 128.
    job.answer_hash = Some(answer_hash);
    job.logprobs_hash = Some(logprobs_hash);
    job.answer_tokens = Some(answer_tokens);
    job.state = JobState::Answered;
    JOBS.save(deps.storage, job_id, &job)?;
    Ok(Response::new()
        .add_attribute("action", "submit_answer")
        .add_attribute("job_id", job_id.to_string()))
}

fn submit_check(
    deps: DepsMut,
    info: MessageInfo,
    job_id: u64,
    divergence_micro: Micro,
    logprobs_hash: String,
) -> Result<Response, ContractError> {
    let cfg = CONFIG.load(deps.storage)?;
    let job = JOBS
        .may_load(deps.storage, job_id)?
        .ok_or(ContractError::JobUnknown)?;
    if job.state != JobState::Answered {
        return Err(ContractError::WrongJobState {
            state: format!("{:?}", job.state),
            expected: "Answered".into(),
        });
    }
    // Исполнитель не проверяет себя. Без этого четыре «независимые»
    // проверки мог бы сдать он сам.
    if job.executor.as_ref() == Some(&info.sender) {
        return Err(ContractError::SelfCheck);
    }
    // Проверяющий обязан пройти ту же пробу, что и исполнитель.
    //
    // Замерено: узел в неверном режиме исполнения расходится с эталоном
    // до 413 488 микродолей, оставаясь той же моделью, версией и dtype.
    // У исполнителя это выглядит подлогом, а у ПРОВЕРЯЮЩЕГО — обвинением
    // честного: его расхождение завышено не потому, что ответ плохой.
    // Правило «виновен, если превысили все» от этого не спасает — четыре
    // неверно настроенных проверяющих вынесут вердикт согласованно.
    //
    // Цена этого решения: проверять теперь может не кто угодно, а
    // зарегистрированный и прошедший пробу узел. Залога с проверяющего
    // не требуется — это отдельный вопрос.
    conformant(deps.as_ref(), &cfg, &info.sender)?;
    if CHECKS.has(deps.storage, (job_id, &info.sender)) {
        return Err(ContractError::DuplicateCheck);
    }
    let have = count_checks(deps.as_ref(), job_id)?;
    if have >= cfg.checks_required {
        return Err(ContractError::ChecksComplete);
    }
    CHECKS.save(
        deps.storage,
        (job_id, &info.sender),
        &Check {
            checker: info.sender.clone(),
            divergence_micro,
            logprobs_hash,
            honest: None,
        },
    )?;
    Ok(Response::new()
        .add_attribute("action", "submit_check")
        .add_attribute("job_id", job_id.to_string())
        .add_attribute("collected", (have + 1).to_string()))
}

/// Прошёл ли узел действующую пробу. Применяется и к исполнителю, и к
/// проверяющему: сравниваются их числа, значит настройка у них должна
/// быть одна.
fn conformant(deps: Deps, cfg: &Config, who: &Addr) -> Result<(), ContractError> {
    let e = EXECUTORS
        .may_load(deps.storage, who)?
        .ok_or(ContractError::ExecutorUnknown)?;
    if e.status == ExecutorStatus::Banned {
        return Err(ContractError::ExecutorBanned);
    }
    match &e.conformance {
        None => Err(ContractError::ConformanceMissing),
        Some(c) if c.ref_version != cfg.conformance_version => {
            Err(ContractError::ConformanceStale {
                have: c.ref_version,
                want: cfg.conformance_version,
            })
        }
        Some(_) => Ok(()),
    }
}

fn count_checks(deps: Deps, job_id: u64) -> StdResult<u32> {
    Ok(CHECKS
        .prefix(job_id)
        .range(deps.storage, None, None, Order::Ascending)
        .count() as u32)
}

fn load_checks(deps: Deps, job_id: u64) -> StdResult<Vec<Check>> {
    CHECKS
        .prefix(job_id)
        .range(deps.storage, None, None, Order::Ascending)
        .map(|r| r.map(|(_, c)| c))
        .collect()
}

/// Правило: виновен, только если хэш вектора НЕ совпал у всех
/// проверяющих. Порога больше нет.
///
/// ПОЧЕМУ НЕ ПОРОГ. Замерено на корпусе из 662 различных запросов:
/// честное расхождение между архитектурами доходит до 37 683, а подлог
/// FP8 начинается с 19 629 — полосы пересекаются, отношение 0,52x.
/// Порога между ними не существует, и прежние 36 000 лежали внутри
/// обеих полос. Прежнее разделение держалось на наборе из сорока
/// запросов и было свойством набора, а не правила. Отсечка по длине и
/// другие статистики (медиана, p99, максимум, доля токенов выше порога,
/// корень из среднего квадрата) не спасают ни одна.
///
/// ПОЧЕМУ РАВЕНСТВО РАБОТАЕТ. Внутри класса — та же архитектура и тот же
/// закреплённый кеш ядер — честные узлы дают ПОБИТОВО ОДИНАКОВЫЕ
/// векторы: 334 из 334 текстов на второй карте, другой машине и другом
/// драйвере. А всё, что меняет арифметику, не совпадает ни на одном
/// тексте: неверный режим, FP8, AWQ4.
///
/// ДВЕ СТУПЕНИ, А НЕ ОДНА. Чужая связка даёт не обвинение, а ОТСУТСТВИЕ
/// вердикта: такой узел не проходит пробу и до проверки не допускается
/// (см. `conformant`). Обвиняет только несовпадение хэша у того, кто
/// пробу прошёл. Иначе один бит, набежавший от патча рантайма или сбоя
/// памяти, разорял бы честного — а при нулевом допуске такой бит
/// возможен: тот же разный блок свёртки уводит Ampere на 20 053, а Ada
/// всего на 6 миллионных, и обе величины одинаково ломают равенство.
fn finalize(deps: DepsMut, job_id: u64) -> Result<Response, ContractError> {
    let cfg = CONFIG.load(deps.storage)?;
    let mut job = JOBS
        .may_load(deps.storage, job_id)?
        .ok_or(ContractError::JobUnknown)?;
    if job.state != JobState::Answered {
        return Err(ContractError::WrongJobState {
            state: format!("{:?}", job.state),
            expected: "Answered".into(),
        });
    }
    if job.paid {
        return Err(ContractError::AlreadyPaid);
    }
    let checks = load_checks(deps.as_ref(), job_id)?;
    if (checks.len() as u32) < cfg.checks_required {
        return Err(ContractError::NotEnoughChecks {
            got: checks.len() as u32,
            need: cfg.checks_required,
        });
    }
    // Обязательство исполнителя. Без него сравнивать не с чем, и это не
    // «нет вердикта», а порченое задание: вектор обязан быть сдан.
    let commitment = job
        .logprobs_hash
        .clone()
        .ok_or(ContractError::EmptyVector)?;
    let mismatched = checks
        .iter()
        .filter(|c| c.logprobs_hash != commitment)
        .count() as u32;
    let guilty = mismatched == cfg.checks_required;

    let executor = job.executor.clone().ok_or(ContractError::NotYourJob)?;
    let mut e = EXECUTORS
        .may_load(deps.storage, &executor)?
        .ok_or(ContractError::ExecutorUnknown)?;

    let mut msgs: Vec<CosmosMsg> = vec![];

    // Проверяющие получают своё НЕЗАВИСИМО ОТ ВЕРДИКТА. Это не щедрость:
    // плати им из исхода — появится интерес к исходу. Им платят за то,
    // что проверка состоялась.
    // Плата взята ИЗ ЗАДАНИЯ, а не пересчитана по текущей доле: её могли
    // сменить после постановки, а деньги уже внесены.
    let fee_total = job.fee_total;
    let share = fee_total / Uint128::from(checks.len() as u128);
    if !share.is_zero() {
        for c in &checks {
            msgs.push(pay(&cfg, &c.checker, share)?);
        }
    }
    let paid_fees = share * Uint128::from(checks.len() as u128);
    // Цена достаётся получателю ЦЕЛИКОМ: долю заказчик внёс сверху, и
    // повышение доли голосованием не урезает выручку исполнителя.
    let rest = job.price;
    // Остаток платы, не разошедшийся по долям, возвращается ЗАКАЗЧИКУ.
    // Он внёс его за проверку; не разошёлся — значит проверка его не
    // получила, и оставлять его исполнителю не за что. В обычном случае
    // остатка нет вовсе: доля делится без него.
    let fee_left = fee_total - paid_fees;

    if guilty {
        e.convictions += 1;
        e.status = ExecutorStatus::Banned;
        // Заказчику возвращается цена: ответ он получил негодный. Плата
        // проверяющим не возвращается — проверка работу сделала, и
        // платят им независимо от вердикта.
        let back = rest + fee_left;
        if !back.is_zero() {
            msgs.push(pay(&cfg, &job.client, back)?);
        }
        // Изъятие залога. Проверяющим он не достаётся ни при каком
        // устройстве настроек — варианта такого нет.
        if !e.stake.is_zero() {
            let amount = e.stake;
            e.stake = Uint128::zero();
            msgs.push(match &cfg.forfeit {
                Forfeit::Burn => BankMsg::Burn {
                    amount: vec![Coin {
                        denom: cfg.stake_denom.clone(),
                        amount,
                    }],
                }
                .into(),
                Forfeit::ToAddress(to) => BankMsg::Send {
                    to_address: to.to_string(),
                    amount: vec![Coin {
                        denom: cfg.stake_denom.clone(),
                        amount,
                    }],
                }
                .into(),
            });
        }
    } else {
        e.jobs_done += 1;
        if !rest.is_zero() {
            msgs.push(pay(&cfg, &executor, rest)?);
        }
        if !fee_left.is_zero() {
            msgs.push(pay(&cfg, &job.client, fee_left)?);
        }
    }
    EXECUTORS.save(deps.storage, &executor, &e)?;

    job.guilty = Some(guilty);
    job.state = JobState::Settled;
    job.paid = true;
    JOBS.save(deps.storage, job_id, &job)?;

    Ok(Response::new()
        .add_messages(msgs)
        .add_attribute("action", "finalize")
        .add_attribute("job_id", job_id.to_string())
        .add_attribute("mismatched", mismatched.to_string())
        .add_attribute("guilty", guilty.to_string())
        .add_attribute("verifier_fees", paid_fees.to_string())
        .add_attribute("fee_returned", fee_left.to_string())
        .add_attribute("settled", rest.to_string()))
}


/// Разбор спора по одной проверке.
///
/// Раскрываются оба вектора, оба сверяются со своими обязательствами, и
/// расхождение ПЕРЕСЧИТЫВАЕТСЯ контрактом. Это закрывает дыру, которую
/// оставляет SubmitCheck: там проверяющий сообщает число, и до появления
/// обязательства хэшем оспорить его было нечем.
///
/// Кто вызывает — безразлично: исход определяют сами векторы. Поэтому и
/// не требуется ни залога спорщика, ни права на спор.
fn resolve_dispute(
    deps: DepsMut,
    job_id: u64,
    checker: String,
    producer_logprobs: Vec<MicroLog>,
    checker_logprobs: Vec<MicroLog>,
) -> Result<Response, ContractError> {
    let mut job = JOBS
        .may_load(deps.storage, job_id)?
        .ok_or(ContractError::JobUnknown)?;
    // До Finalize: после сведения вердикт применён, исполнитель уже
    // забанен или зачтён, и разбирать поздно.
    if job.state == JobState::Settled {
        return Err(ContractError::AlreadySettled);
    }
    if job.state != JobState::Answered {
        return Err(ContractError::WrongJobState {
            state: format!("{:?}", job.state),
            expected: "Answered".into(),
        });
    }
    let want_producer = job
        .logprobs_hash
        .clone()
        .ok_or(ContractError::EmptyVector)?;
    let got_producer = commit_hash(&producer_logprobs);
    if got_producer != want_producer {
        return Err(ContractError::RevealMismatch {
            got: got_producer,
            want: want_producer,
        });
    }

    let addr = deps.api.addr_validate(&checker)?;
    let mut check = CHECKS
        .may_load(deps.storage, (job_id, &addr))?
        .ok_or(ContractError::CheckUnknown {
            checker: checker.clone(),
        })?;
    let got_checker = commit_hash(&checker_logprobs);
    if got_checker != check.logprobs_hash {
        return Err(ContractError::RevealMismatch {
            got: got_checker,
            want: check.logprobs_hash.clone(),
        });
    }

    let recomputed = divergence_micro(&producer_logprobs, &checker_logprobs)?;
    // Честен, если заявленное совпало с пересчитанным. Без допуска:
    // оба считают по одному канону из одних целых чисел, расхождение
    // возможно только от обмана или от ошибки в демоне.
    let honest = recomputed == check.divergence_micro;
    check.honest = Some(honest);
    CHECKS.save(deps.storage, (job_id, &addr), &check)?;

    job.reveal_verified = Some(true);
    JOBS.save(deps.storage, job_id, &job)?;

    Ok(Response::new()
        .add_attribute("action", "resolve_dispute")
        .add_attribute("job_id", job_id.to_string())
        .add_attribute("checker", addr)
        .add_attribute("claimed", check.divergence_micro.to_string())
        .add_attribute("recomputed", recomputed.to_string())
        .add_attribute("honest", honest.to_string()))
}

#[entry_point]
pub fn query(deps: Deps, _env: Env, msg: QueryMsg) -> StdResult<Binary> {
    match msg {
        QueryMsg::Config {} => to_json_binary(&CONFIG.load(deps.storage)?),
        QueryMsg::Executor { address } => {
            let addr = deps.api.addr_validate(&address)?;
            to_json_binary(&EXECUTORS.load(deps.storage, &addr)?)
        }
        QueryMsg::Job { job_id } => to_json_binary(&JOBS.load(deps.storage, job_id)?),
        QueryMsg::Checks { job_id } => {
            let checks = load_checks(deps, job_id)?;
            let job = JOBS.load(deps.storage, job_id)?;
            let mismatched = match job.logprobs_hash {
                Some(h) => checks.iter().filter(|c| c.logprobs_hash != h).count() as u32,
                None => 0,
            };
            to_json_binary(&ChecksResponse { checks, mismatched })
        }
    }
}
