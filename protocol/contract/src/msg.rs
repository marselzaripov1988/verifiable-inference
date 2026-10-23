use cosmwasm_schema::{cw_serde, QueryResponses};
use cosmwasm_std::{Binary, Uint128};

use crate::state::{Check, Forfeit, Micro, MicroLog, Settlement};

#[cw_serde]
pub struct InstantiateMsg {
    pub admin: String,
    pub attestor: String,
    pub checks_required: u32,
    pub pinned_model: String,
    pub pinned_runtime: String,
    pub pinned_dtype: String,
    pub pinned_arch: String,
    pub kernel_cache_hash: String,
    pub conformance_limit_micro: Micro,
    pub conformance_ref_hash: String,
    pub conformance_lens: Vec<u32>,
    pub settlement: Settlement,
    pub stake_denom: String,
    pub min_stake: Uint128,
    pub verifier_fee_bps: u16,
    pub forfeit: Forfeit,
}

#[cw_serde]
pub enum ExecuteMsg {
    /// Админ меняет настройки. Смена порога или связки модель/рантайм
    /// допустима только вместе с перезамером — контракт этого не
    /// проверяет и проверить не может, поэтому право у админа.
    UpdateConfig {
        /// Сменить администратора. Нужно затем, что без этого потеря
        /// ключа навсегда замораживает ВСЕ параметры: ни порог, ни
        /// эталон пробы, ни долю уже не поправить.
        ///
        /// Передача не туда запирает так же надёжно — поэтому адрес
        /// проверяется, а менять его стоит в последнюю очередь, убедившись
        /// в новом ключе.
        admin: Option<String>,
        attestor: Option<String>,
        checks_required: Option<u32>,
        pinned_model: Option<String>,
        pinned_runtime: Option<String>,
        pinned_dtype: Option<String>,
        pinned_arch: Option<String>,
        kernel_cache_hash: Option<String>,
        conformance_limit_micro: Option<Micro>,
        /// Новый эталон пробы. Меняется только целиком — хэш и длины
        /// вместе, иначе раскрытую склейку разрежут не по тем границам.
        /// Повышает conformance_version и обнуляет все выданные пробы.
        conformance_ref: Option<ConformanceRef>,
        settlement: Option<Settlement>,
        stake_denom: Option<String>,
        min_stake: Option<Uint128>,
        verifier_fee_bps: Option<u16>,
        forfeit: Option<Forfeit>,
    },
    /// Исполнитель заявляет о себе. Связка обязана совпасть с прибитой.
    /// Статус после этого Pending: заданий не получает, пока допуск не
    /// подтверждён.
    RegisterExecutor {
        model: String,
        runtime: String,
        dtype: String,
        card_class: String,
        /// Какой кеш ядер поставил узел. Обязан совпасть с настройкой:
        /// без общего кеша узлы класса считают по-разному в последнем
        /// бите, и сверка хэшей невозможна.
        kernel_cache_hash: String,
    },
    /// Подтверждение допуска. Только от attestor — см. state::Config.
    Attest { executor: String, eligible: bool },
    /// Отстранить (обратимо) и вернуть. Только админ.
    SetSuspended { executor: String, suspended: bool },
    /// Исполнитель вносит залог в stake_denom. Монеты идут с транзакцией.
    DepositStake {},
    /// Забрать залог. Нельзя забаненному и нельзя при незакрытых
    /// заданиях: иначе залог уходит ровно перед вердиктом.
    WithdrawStake { amount: Uint128 },
    /// Заказчик ставит задание и платит РОДНОЙ монетой — она приходит с
    /// транзакцией. Работает только при расчёте Native.
    ///
    /// `price` — сколько получит исполнитель. Долю проверяющих заказчик
    /// платит СВЕРХУ, поэтому с транзакцией должно прийти ровно
    /// `price + доля`.
    SubmitJob { prompt_hash: String, price: Uint128 },
    /// Приём CW20. При расчёте Cw20 заказчик НЕ вызывает SubmitJob
    /// напрямую: монеты приходят только так — Send на контракт с
    /// вложенным сообщением. Это цена того, что расчётная единица не
    /// нативна.
    Receive(Cw20ReceiveMsg),
    /// Исполнитель сдаёт пробу на эталонном векторе: максимум
    /// расхождения по текстам и обязательство своими векторами.
    ///
    /// Сдаётся ПОСЛЕ регистрации и до первого задания. Сам контракт
    /// порождать нечего не может, поэтому считает узел, а контракт
    /// отклоняет число выше предела и хранит обязательство, по которому
    /// любой может его опровергнуть.
    SubmitConformance {
        divergence_micro: Micro,
        logprobs_hash: String,
    },
    /// Опровергнуть пробу: раскрыть эталон и векторы исполнителя,
    /// сверить оба с обязательствами и ПЕРЕСЧИТАТЬ максимум по текстам.
    ///
    /// Вызвать может кто угодно. В отличие от спора по заданию, здесь
    /// вердикт не требует ничьей оценки: оба набора зафиксированы
    /// хэшами, пересчёт однозначен. Поэтому и наказание прямое — у
    /// исполнителя было всё, чтобы назвать верное число.
    DisputeConformance {
        executor: String,
        reference_logprobs: Vec<MicroLog>,
        executor_logprobs: Vec<MicroLog>,
    },
    /// Исполнитель берёт задание.
    ClaimJob { job_id: u64 },
    /// Исполнитель сдаёт ответ: хэш ответа, хэш вектора вероятностей и
    /// длину ответа в токенах. Сам вектор вне цепочки.
    SubmitAnswer {
        job_id: u64,
        answer_hash: String,
        logprobs_hash: String,
        answer_tokens: u32,
    },
    /// Проверяющий сдаёт расхождение И обязательство своим вектором.
    /// Без хэша расхождение — его слово против слова исполнителя.
    SubmitCheck {
        job_id: u64,
        divergence_micro: Micro,
        logprobs_hash: String,
    },
    /// Свести проверки в вердикт. Можно вызвать кому угодно: решение
    /// определяется собранными проверками, а не тем, кто нажал.
    Finalize { job_id: u64 },
    /// Разобрать спор по одной проверке: раскрыть оба вектора, сверить их
    /// с обязательствами и ПЕРЕСЧИТАТЬ расхождение.
    ///
    /// Вызвать может кто угодно — вердикт определяется самими векторами.
    /// Разбирается до Finalize: после сведения вердикт уже применён.
    ResolveDispute {
        job_id: u64,
        checker: String,
        producer_logprobs: Vec<MicroLog>,
        checker_logprobs: Vec<MicroLog>,
    },
}

/// Эталон пробы: обязательство набором и границы текстов в нём.
#[cw_serde]
pub struct ConformanceRef {
    pub ref_hash: String,
    pub lens: Vec<u32>,
}

/// То, что заказчик кладёт в Send как msg.
#[cw_serde]
pub enum ReceiveHook {
    /// `price` — сколько получит исполнитель; переслать надо
    /// `price + доля`.
    SubmitJob { prompt_hash: String, price: Uint128 },
}

/// Исходящее в контракт CW20. Объявлено здесь по той же причине, что и
/// Cw20ReceiveMsg: формат фиксирован стандартом, зависимость не нужна.
#[cw_serde]
pub enum Cw20Execute {
    Transfer { recipient: String, amount: Uint128 },
    Burn { amount: Uint128 },
}

/// Приходит от контракта CW20. Объявлен здесь, а не взят из cw20 —
/// зависимость ради трёх полей не нужна, а формат фиксирован стандартом.
#[cw_serde]
pub struct Cw20ReceiveMsg {
    pub sender: String,
    pub amount: Uint128,
    pub msg: Binary,
}

#[cw_serde]
#[derive(QueryResponses)]
pub enum QueryMsg {
    #[returns(crate::state::Config)]
    Config {},
    #[returns(crate::state::Executor)]
    Executor { address: String },
    #[returns(crate::state::Job)]
    Job { job_id: u64 },
    #[returns(ChecksResponse)]
    Checks { job_id: u64 },
}

#[cw_serde]
pub struct ChecksResponse {
    pub checks: Vec<Check>,
    /// У скольких из сданных хэш НЕ совпал с обязательством исполнителя.
    /// Удобно для наблюдения, но вердикт считается в Finalize, а не
    /// здесь.
    pub mismatched: u32,
}
