import json
import uuid
import time
import threading
import requests
from typing import Optional, Dict, Any

from app.utils.commons import SingletonMeta
from config import Config
import log

def generate_tab_id() -> str:
    """Generate a unique tab ID."""
    return str(uuid.uuid4())


class DrissionPageHelper(metaclass=SingletonMeta):
    
    # CF 修复的全局锁和状态（类级别，所有线程共享）
    _cf_solve_lock = threading.Lock()
    _cf_solving = False
    _cf_last_solve_time = 0
    _cf_solve_cooldown = 30  # 上次修复成功后 30 秒内不重复修复

    def __init__(self):
        self.url = ""
        url = Config().get_config("laboratory").get('chrome_server_host')
        if url:
            self.url = url.rstrip('/')

    def get_status(self) -> bool:
        """检查 Chrome 服务器连接状态，只有连接成功才返回 True"""
        if not self.url:
            return False
        
        try:
            # 测试连接状态
            response = self._request_with_retry(
                method="GET",
                url=f"{self.url}/status",
                timeout=5
            )
            # 如果响应状态码为 200，表示连接成功
            return response.status_code == 200
        except Exception as e:
            log.warn(f"Chrome 服务器连接失败: {str(e)}")
            return False

    def _request_with_retry(self, 
                           method: str, 
                           url: str, 
                           retries: int = 3, 
                           delay: int = 2, 
                           **kwargs) -> requests.Response:
        """通用的网络请求重试逻辑"""
        for attempt in range(retries):
            try:
                response = requests.request(method, url, **kwargs)
                return response
            except requests.exceptions.RequestException as e:
                log.warn(f"请求失败(重试 {attempt + 1}): {e}")
                if attempt < retries - 1:
                    time.sleep(delay)
                else:
                    log.error(f"所有重试失败，失败请求 {url}")
                    raise

    def get_page_html(self,
                      url: str,
                      cookies: Optional[str] = None,
                      local_storage: Optional[Dict[str, Any]] = None,
                      timeout: int = 120,
                      click_xpath: Optional[str] = None,
                      delay: int = 5,
                      click_delay: Optional[int] = None) -> str:
        """获取HTML内容，带超时保护
        
        Args:
            url: 页面URL
            cookies: Cookie字符串
            local_storage: LocalStorage字典数据
            timeout: 超时时间（秒）
            click_xpath: 点击元素的XPath
            delay: 页面加载延迟时间（秒）
            click_delay: 点击后等待时间（秒），如果为None则使用delay
        """
        if not self.get_status():
            return ""
        
        headers = {"Content-Type": "application/json"}
        tab_id = self.create_tab(url=url, cookies=cookies, local_storage=local_storage, timeout=timeout)
        if not tab_id:
            return ""

        # 设置点击后等待时间，默认为delay
        actual_click_delay = click_delay if click_delay is not None else delay
        
        # 创建超时线程
        timeout_occurred = False
        
        def timeout_handler():
            nonlocal timeout_occurred
            # 计算总超时时间：基础超时 + 延迟时间 + 点击后等待时间（如果有点击）
            total_timeout = timeout + delay + (actual_click_delay if click_xpath else 0) + 10
            time.sleep(total_timeout)
            if not timeout_occurred:
                log.warn(f"标签页 {tab_id} 超时，强制关闭")
                self.close_tab(tab_id)
        
        timeout_thread = threading.Thread(target=timeout_handler)
        timeout_thread.daemon = True
        timeout_thread.start()

        try:
            # 初始页面加载等待
            time.sleep(delay)
            
            # 获取初始HTML内容
            html_url = f"{self.url}/tabs/{tab_id}/html"
            res_json = self._fetch_html(html_url, timeout)
            
            # 处理点击事件
            if click_xpath:
                click_url = f"{self.url}/tabs/click/"
                click_data = json.dumps({
                    "tab_name": tab_id,
                    "selector": click_xpath
                }, separators=(',', ':'))
                
                try:
                    response = self._request_with_retry(
                        method="POST",
                        url=click_url,
                        headers=headers,
                        data=click_data,
                        timeout=timeout
                    )
                    # 点击后等待页面更新 - 使用专门的点击后等待时间
                    log.info(f"点击完成，等待 {actual_click_delay} 秒让页面加载")
                    time.sleep(actual_click_delay)
                    # 获取点击后的HTML内容
                    res_json = self._fetch_html(html_url, timeout)
                except Exception as e:
                    log.error(f"点击标签页失败: {str(e)}")
                    self.close_tab(tab_id)
                    return ""

            # 解析HTML内容
            html_dict = json.loads(res_json)
            content = html_dict.get("html", "")
            
            # 标记操作完成，避免超时线程强制关闭
            timeout_occurred = True
            
            return content
            
        except Exception as e:
            log.error(f"获取页面HTML失败: {str(e)}")
            timeout_occurred = True
            return ""
        finally:
            # 确保标签页被关闭
            self.close_tab(tab_id)
            timeout_occurred = True

    def get_page_html_without_closetab(self, 
                                       tab_id: str, 
                                       timeout: int = 20, 
                                       is_refresh: bool = False,
                                       cf: bool = False,
                                       tab_category: str = "html") -> str:
        """
        获取html并保持标签页打开
        """
        
        if not self.get_status():
            return ""

        if is_refresh:
            self._refresh_tab(tab_id=tab_id)
        # 获取html内容
        html_url = f"{self.url}/tabs/{tab_id}/{tab_category}?cf={cf}"
        try:
            res_json = self._fetch_html(html_url, timeout)
        except Exception as e:
            log.error(f"获取html失败: {str(e)}")
            self.close_tab(tab_id)
            return ""

        html_dict = json.loads(res_json)
        content = html_dict.get("html")
        return content

    def _fetch_html(self, url: str, timeout: int) -> str:
        """获取HTML内容并返回JSON字符串"""
        try:
            response = self._request_with_retry(
                method="GET",
                url=url,
                timeout=timeout
            )
            return response.text
        except Exception as e:
            log.error(f"_fetch_html 失败: {str(e)}")
            raise

    def _parse_html_response(self, response_text: str) -> str:
        """解析HTML响应，提取HTML内容"""
        try:
            html_dict = json.loads(response_text)
            return html_dict.get("html", "")
        except json.JSONDecodeError as e:
            log.error(f"解析HTML响应失败: {str(e)}")
            return ""

    def create_tab(self, url: str, cookies: Optional[str] = None, local_storage: Optional[Dict[str, Any]] = None, timeout: int = 20) -> str:
        """创建新标签页
        
        Args:
            url: 页面URL
            cookies: Cookie字符串
            local_storage: LocalStorage字典数据
            timeout: 超时时间（秒）
        """
        if not self.get_status():
            return ""

        headers = {"Content-Type": "application/json"}
        tab_id = generate_tab_id()

        # 构建请求数据
        open_tab_data = {
            "url": url,
            "tab_name": tab_id,
            "cookie": cookies
        }
        
        # 如果有local_storage数据，添加到请求中
        if local_storage:
            open_tab_data["local_storage"] = local_storage

        # 打开新标签
        tabs_url = f"{self.url}/tabs"
        try:
            response = self._request_with_retry(
                method="POST",
                url=tabs_url,
                headers=headers,
                data=json.dumps(open_tab_data, separators=(',', ':')),
                timeout=timeout
            )
            if response.status_code not in (200, 400): 
                log.error(f"打开新标签页失败: {response.text}")
                return ""
            return tab_id
        except Exception as e:
            log.error(f"url: {url} 打开新标签页失败: {str(e)}")
            self.close_tab(tab_id=tab_id)
            return ""

    def get_cookie(self, tab_id: str, timeout: int = 20) -> str:
        """返回cookie"""
        # 延迟加载，等待网页渲染完成
        if not self.get_status():
            return ""

        try:
            response = self._request_with_retry(
                method="GET",
                url=f"{self.url}/tabs/{tab_id}/cookie",
                timeout=timeout
            )
            
            cookie_dict = response.json()
            content = cookie_dict.get("cookie")
            return content
        except Exception as e:
            log.error(f"get_cookie 失败: {str(e)}")
            raise

    def close_tab(self, tab_id: str):
        """关闭标签页"""
        
        if not self.get_status():
            return

        close_url = f"{self.url}/tabs/{tab_id}"
        try:
            self._request_with_retry(method="DELETE", url=close_url)
        except Exception as e:
            log.error(f"关闭标签页异常: {str(e)}")
            
    def _refresh_tab(self, tab_id: str):
        """刷新标签页"""
        try:
            response = self._request_with_retry(
                method="GET",
                url=f"{self.url}/tabs/{tab_id}/refresh"
            )
            
            status_dict = response.json()
            status = status_dict.get("code")
            return status
        except Exception as e:
            log.error(f"_refresh_tab 失败: {str(e)}")
            raise
        
    def input_on_element(self, tab_id: str, selector: str, input_str: str, timeout: int = 20) -> bool:
        """在元素上输入文本"""
        if not self.get_status():
            return False

        headers = {"Content-Type": "application/json"}  
        click_url = f"{self.url}/tabs/input/"
        click_data = json.dumps({
            "tab_name": tab_id,
            "selector": selector,
            "input_str": input_str
        }, separators=(',', ':'))
        try:
            response = self._request_with_retry(
                method="POST",
                url=click_url,
                headers=headers,
                data=click_data,
                timeout=timeout
            )
            if response.json().get('code') == 0:
                return True
        except Exception as e:
            log.error(f"输入失败: {str(e)}")
            self.close_tab(tab_id=tab_id)
            return False
        return False

    def close_all_tabs(self):
        """关闭标签页"""
        if not self.get_status():
            return

        close_url = f"{self.url}/tabs/"
        try:
            self._request_with_retry(method="DELETE", url=close_url)
        except Exception as e:
            log.error(f"关闭标签页异常: {str(e)}")

    def solve_cf_for_site(self, site_url: str, cookies: Optional[str] = None, timeout: int = 120) -> bool:
        """
        为指定站点解决 CF 验证（线程安全）。
        多个搜索线程可能同时检测到 CF 拦截，但只允许一个线程执行修复，
        其他线程等待修复完成后直接返回结果。
        
        流程优化说明：
        跳过 get_tab_html 步骤（该步骤会触发 chrome 端的嵌入式 Turnstile 处理，
        但因为 shadow DOM 渲染慢，经常浪费大量时间且失败）。
        改为：创建标签页后等待足够时间让 Turnstile 组件渲染，然后直接调用 /solve_cf 接口。
        
        Args:
            site_url: 需要过盾的页面 URL
            cookies: 站点 Cookie
            timeout: 超时时间
            
        Returns:
            True 表示 CF 验证成功
        """
        if not self.get_status():
            return False

        # 如果最近刚修复成功，直接返回（避免重复修复）
        if time.time() - self._cf_last_solve_time < self._cf_solve_cooldown:
            log.info(f"【CF修复】最近已修复成功，跳过重复修复")
            return True

        # 尝试获取锁，如果已有线程在修复则等待
        acquired = self._cf_solve_lock.acquire(timeout=timeout)
        if not acquired:
            log.warn(f"【CF修复】等待锁超时")
            return False

        try:
            # 获取锁后再检查一次，可能等待期间已被其他线程修复
            if time.time() - self._cf_last_solve_time < self._cf_solve_cooldown:
                log.info(f"【CF修复】其他线程已完成修复，直接返回")
                return True

            self._cf_solving = True
            log.info(f"【CF修复】开始为 {site_url} 处理 CF 验证...")

            # 创建标签页访问站点主页，create_tab 内部会自动处理全屏 CF 拦截
            tab_id = self.create_tab(url=site_url, cookies=cookies, timeout=timeout)
            if not tab_id:
                log.error(f"【CF修复】创建标签页失败: {site_url}")
                return False

            cf_solved = False
            try:
                # 等待页面加载和 Turnstile 组件渲染
                # 跳过 get_tab_html（会触发无效的嵌入式 Turnstile 处理，浪费时间）
                # 直接等待足够时间让 shadow DOM 渲染完成
                log.info(f"【CF修复】等待 Turnstile 组件渲染...")
                time.sleep(15)

                # 直接调用 /solve_cf 接口，此时组件应已渲染完成
                log.info(f"【CF修复】调用 solve_cf 接口...")
                try:
                    solve_url = f"{self.url}/tabs/{tab_id}/solve_cf"
                    response = self._request_with_retry(
                        method="POST",
                        url=solve_url,
                        timeout=timeout
                    )
                    result = response.json()
                    if result.get("success"):
                        cf_solved = True
                    else:
                        # 第一次失败，可能组件还没渲染好，再等一次
                        log.info(f"【CF修复】首次尝试未成功({result.get('message')}), 等待后重试...")
                        time.sleep(10)
                        
                        response = self._request_with_retry(
                            method="POST",
                            url=solve_url,
                            timeout=timeout
                        )
                        result = response.json()
                        if result.get("success"):
                            cf_solved = True
                        else:
                            log.warning(f"【CF修复】✗ CF 验证处理失败: {result.get('message')}")
                except Exception as e:
                    log.error(f"【CF修复】调用 solve_cf 失败: {e}")

                if cf_solved:
                    # 点击 Turnstile 后需等待 cf_clearance cookie 写入完成，再关闭标签页
                    # 过早关闭会中断 token 验证的网络往返，导致 cookie 未生效
                    log.info(f"【CF修复】✓ CF 验证已解决，等待 cookie 写入: {site_url}")
                    time.sleep(5)
                    self._cf_last_solve_time = time.time()
                    return True
                return False
            finally:
                self.close_tab(tab_id)

        finally:
            self._cf_solving = False
            self._cf_solve_lock.release()
