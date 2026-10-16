import { redirect } from 'next/navigation';

/** 平台外壳首页: 统一落到仪表盘(聊天在 /chat)。 */
const HomePage = () => {
  redirect('/dashboard');
};

export default HomePage;
